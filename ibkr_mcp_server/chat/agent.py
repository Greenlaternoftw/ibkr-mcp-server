"""Anthropic-API agentic loop for the chat wrapper.

Single entry point: ``AnthropicAgent.run(messages)``.

  - Sends `messages` + tool catalog to Anthropic API.
  - If the response stops with `tool_use`: execute each tool, append
    `tool_result` blocks, loop.
  - If the response stops with `end_turn`: return the assistant message
    so the caller can append + display.
  - If iteration cap is hit before `end_turn`: raise ChatError -- this
    is the runaway-loop safety net.

The agent receives a `tool_dispatcher` callable so it doesn't have to
care whether tools are coming from MCP, a mock, or a future direct
IBKRClient binding. Loose coupling lets tests swap in fakes without
having to spin up the MCP server.

Cost-tracking: the API client returns `usage.input_tokens` and
`usage.output_tokens` on each call. We accumulate and surface them on
the AgentResult so callers can log or display.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


class ChatError(Exception):
    """Surfaced when the agent loop can't produce a clean reply.

    Covers: API errors from Anthropic, iteration-cap hit, malformed
    tool-use blocks. The HTTP route turns this into a 4xx/5xx JSON
    error for the browser.
    """


@dataclass
class StreamEvent:
    """One event in the chat stream that the HTTP layer turns into SSE.

    Event types and their payloads:

      ``text``         delta of model output text (one chunk per token-ish unit)
                       payload: ``{"delta": "..."}``
      ``tool_call``    model is invoking a tool (one event per tool_use block,
                       fired AFTER the stream completes for that turn so input
                       is the full JSON, not a partial)
                       payload: ``{"name": "...", "input": {...}, "id": "..."}``
      ``tool_result``  our dispatcher returned. preview is truncated --
                       avoid streaming a 50K-token portfolio response back
                       to the browser when the model is the only consumer.
                       payload: ``{"id": "...", "ok": true/false, "preview": "..."}``
      ``done``         turn complete. carries final conversation + usage so
                       the client can persist and update token counters.
                       payload: ``{"conversation": [...], "usage": {...}}``
      ``error``        unrecoverable error during the turn.
                       payload: ``{"message": "..."}``
    """

    type: str
    payload: dict

    def to_sse(self) -> str:
        """Render as one ``data: <json>\\n\\n`` SSE message.

        We don't bother with ``event:`` lines -- the client parses ``type``
        out of the JSON instead. Keeps the wire format compact and
        EventSource-compatible in a single shape.
        """
        body = {"type": self.type, **self.payload}
        return f"data: {json.dumps(body)}\n\n"


@dataclass
class AgentResult:
    """One full agent turn's output.

    - ``reply_blocks`` is the assistant's final-turn content blocks
      (text, ready to be rendered).
    - ``conversation`` is the full updated message list (caller persists
      this back as the new thread state).
    - ``input_tokens`` / ``output_tokens`` are accumulated across all
      iterations of this turn.
    - ``cache_creation_input_tokens`` is the number of prompt tokens
      WRITTEN to cache this turn (charged at ~1.25x the input price).
      Non-zero only on the first request after a system-prompt /
      tools-schema change (or after the 5-minute TTL expires).
    - ``cache_read_input_tokens`` is the number of prompt tokens READ
      from cache this turn (charged at ~0.1x the input price). For our
      9.5K fixed prefix (8K tools + 1.5K system prompt), this should
      sit at ~9500 on every request after the first -- if it doesn't,
      a silent cache invalidator is at work (see chat/prompts.py for
      the rules: keep the system prompt static, keep tool order stable).
    - ``iterations`` is how many Anthropic API calls were made (for
      debugging tool-loop behavior).
    """

    reply_blocks: List[dict] = field(default_factory=list)
    conversation: List[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    iterations: int = 0

    def reply_text(self) -> str:
        """Concatenate text blocks for clients that just want a string."""
        out: List[str] = []
        for block in self.reply_blocks:
            if _block_field(block, "type") == "text":
                text = _block_field(block, "text") or ""
                out.append(text)
        return "\n".join(out).strip()


ToolDispatcher = Callable[[str, dict], Awaitable[Any]]
"""Signature for the tool-execution callback.

Receives (tool_name, arguments_dict). Returns whatever the tool returns;
the agent passes it through ``extract_tool_result_text`` to get the
string body for Anthropic's tool_result.
"""


class AnthropicAgent:
    """Self-contained chat agent that talks to Anthropic API + tools."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        tools_schema: List[dict],
        tool_dispatcher: ToolDispatcher,
        max_iterations: int = 12,
        max_tokens: int = 4096,
    ) -> None:
        # Lazy import so unit tests that don't exercise the network path
        # can run without the anthropic package installed.
        import anthropic  # noqa: F401  -- imported for side effect of failing fast

        self._anthropic_module = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.system_prompt = system_prompt
        self.tools_schema = tools_schema
        self.tool_dispatcher = tool_dispatcher
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens

    async def run(self, conversation: List[dict]) -> AgentResult:
        """Run one user turn end-to-end.

        ``conversation`` is the full thread INCLUDING the user message
        that just came in. Returns an AgentResult whose `.conversation`
        is the updated thread (caller should persist it).
        """
        from .schemas import extract_tool_result_content

        # We mutate a copy so a caller-side reference stays clean even
        # if the agent fails mid-loop.
        convo = list(conversation)

        result = AgentResult(conversation=convo)

        for iteration in range(self.max_iterations):
            result.iterations = iteration + 1

            try:
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    # Prompt caching: wrap the system prompt in a list so
                    # we can attach `cache_control`. Render order is
                    # tools -> system -> messages, so a cache marker on
                    # the last system block caches BOTH tools and system
                    # together (~9.5K tokens for us). On a cache hit those
                    # tokens cost ~0.1x the normal input price; on a miss
                    # ~1.25x. The break-even is 2 requests, so this pays
                    # off on every conversation after the first turn.
                    #
                    # DO NOT mutate self.system_prompt or self.tools_schema
                    # mid-process -- any change invalidates the cache for
                    # the rest of the daemon's lifetime.
                    system=[
                        {
                            "type": "text",
                            "text": self.system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=self.tools_schema,
                    messages=convo,
                )
            except Exception as e:  # API error, rate limit, etc.
                logger.exception("Anthropic API call failed")
                raise ChatError(f"Anthropic API call failed: {e}") from e

            # Track token spend (best-effort -- some response shapes may
            # not have usage on errors). Cache fields are non-zero only
            # when prompt caching is engaged; getattr+default keeps the
            # code resilient if Anthropic ever returns a usage object
            # without those fields.
            usage = getattr(response, "usage", None)
            if usage is not None:
                result.input_tokens += getattr(usage, "input_tokens", 0) or 0
                result.output_tokens += getattr(usage, "output_tokens", 0) or 0
                result.cache_creation_input_tokens += (
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                )
                result.cache_read_input_tokens += (
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                )

            # Append the assistant turn to the conversation (Anthropic
            # requires content blocks, not a string, for multi-turn
            # tool-use threads).
            assistant_blocks = [self._block_to_dict(b) for b in response.content]
            convo.append({"role": "assistant", "content": assistant_blocks})

            stop_reason = getattr(response, "stop_reason", None)

            if stop_reason == "tool_use":
                # Execute every tool_use block the model issued, then
                # add a user turn containing the tool_results.
                tool_results = await self._run_tool_blocks(response.content)
                convo.append({"role": "user", "content": tool_results})
                continue

            # end_turn (most common) or any other non-tool stop reason
            # (max_tokens, stop_sequence, etc.) -- we're done.
            result.reply_blocks = assistant_blocks
            return result

        # Iteration cap exceeded. This is rare and usually means a tool
        # is throwing or the model is in a confused loop.
        raise ChatError(
            f"agent loop hit iteration cap ({self.max_iterations}); "
            f"last stop_reason={stop_reason!r}"
        )

    async def run_stream(
        self, conversation: List[dict]
    ) -> AsyncIterator[StreamEvent]:
        """Run one user turn and yield events as they arrive.

        Differences from :meth:`run`:

          * Uses the SDK's ``messages.stream(...)`` context manager so
            text deltas come back as they're generated by the model
            instead of after the whole response is assembled. The browser
            renders them token-by-token -- no more 5-10s "thinking..."
            blank wait.

          * Tool calls and tool results are also surfaced as discrete
            events so the UI can show "calling place_order(...)" / "got
            result" inline while it happens.

          * Same persistent system+tools cache as :meth:`run` (the
            ``cache_control`` marker is identical), and the same
            iteration cap.

        The non-streaming ``run`` method stays available for tests and
        for callers that want one round-trip return-or-nothing semantics.
        """
        convo = list(conversation)
        totals = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
        iteration = 0
        last_stop_reason: Optional[str] = None

        while iteration < self.max_iterations:
            iteration += 1

            # Use the SDK's stream context manager. This yields events
            # for message_start, content_block_start/delta/stop, etc.;
            # we only consume text deltas here for live rendering. The
            # final assembled message is fetched via get_final_message()
            # after the stream closes -- THAT is what we use to compute
            # tool dispatches, append to the conversation, and check
            # stop_reason.
            try:
                async with self._client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": self.system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=self.tools_schema,
                    messages=convo,
                ) as stream:
                    async for text_chunk in stream.text_stream:
                        if text_chunk:
                            yield StreamEvent("text", {"delta": text_chunk})

                    final_msg = await stream.get_final_message()
            except Exception as e:
                logger.exception("Anthropic streaming API call failed")
                yield StreamEvent("error", {"message": f"Anthropic API: {e}"})
                return

            # Accumulate usage. Streaming responses populate usage on
            # the final message just like non-streaming responses do.
            usage = getattr(final_msg, "usage", None)
            if usage is not None:
                totals["input"] += getattr(usage, "input_tokens", 0) or 0
                totals["output"] += getattr(usage, "output_tokens", 0) or 0
                totals["cache_create"] += (
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                )
                totals["cache_read"] += (
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                )

            # Normalize the assistant turn and append it to the conversation.
            assistant_blocks = [self._block_to_dict(b) for b in final_msg.content]
            convo.append({"role": "assistant", "content": assistant_blocks})

            # Surface each tool_use as a discrete event so the UI can render
            # "🔧 get_portfolio({})" inline above the streamed assistant
            # text. Done AFTER the stream so input is the assembled JSON
            # (input_json_delta chunks during streaming are partial).
            for block in assistant_blocks:
                if block.get("type") == "tool_use":
                    yield StreamEvent(
                        "tool_call",
                        {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input") or {},
                        },
                    )

            last_stop_reason = getattr(final_msg, "stop_reason", None)

            if last_stop_reason == "tool_use":
                # Dispatch each tool, then continue the loop with the
                # results appended to the conversation.
                tool_results = await self._run_tool_blocks(final_msg.content)
                convo.append({"role": "user", "content": tool_results})

                for r in tool_results:
                    # Don't ship the full tool output to the browser --
                    # JSON portfolios can be 50KB+ and the model is the
                    # real consumer. Preview is enough for the operator
                    # to see "the call succeeded".
                    #
                    # EXCEPTION: image content (charts). We DO ship the
                    # full base64 PNG so the UI can render it inline --
                    # that's the whole point of the chart tools.
                    content = r.get("content") or ""
                    payload = {
                        "id": r.get("tool_use_id"),
                        "ok": not r.get("is_error", False),
                    }
                    if isinstance(content, list):
                        # Mixed content (text + image). Extract the
                        # first image block's data + take any text as
                        # preview text.
                        text_parts = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text_parts.append(block.get("text") or "")
                            elif block.get("type") == "image":
                                src = block.get("source") or {}
                                if src.get("type") == "base64":
                                    payload["image_b64"] = src.get("data")
                                    payload["image_mime"] = (
                                        src.get("media_type") or "image/png"
                                    )
                        payload["preview"] = "\n".join(text_parts)[:400]
                    else:
                        # Plain string content -- truncate for the preview.
                        payload["preview"] = str(content)[:200]
                    yield StreamEvent("tool_result", payload)
                continue

            # end_turn (most common) or any other non-tool stop -- we're done.
            yield StreamEvent(
                "done",
                {
                    "conversation": convo,
                    "usage": {
                        "input_tokens": totals["input"],
                        "output_tokens": totals["output"],
                        "cache_creation_input_tokens": totals["cache_create"],
                        "cache_read_input_tokens": totals["cache_read"],
                        "iterations": iteration,
                    },
                },
            )
            return

        # Iteration cap hit -- surface as a stream error rather than
        # raising; client renders this inline.
        yield StreamEvent(
            "error",
            {
                "message": (
                    f"agent loop hit iteration cap ({self.max_iterations}); "
                    f"last stop_reason={last_stop_reason!r}"
                )
            },
        )

    async def _run_tool_blocks(self, content_blocks) -> List[dict]:
        """Execute every tool_use block; return the matching tool_result list."""
        from .schemas import extract_tool_result_content

        results: List[dict] = []
        for block in content_blocks:
            if _block_field(block, "type") != "tool_use":
                continue

            tool_use_id = _block_field(block, "id")
            tool_name = _block_field(block, "name")
            tool_input = _block_field(block, "input") or {}

            logger.info(
                "chat agent dispatching tool: %s args=%s", tool_name, _safe_repr(tool_input)
            )

            is_error = False
            try:
                raw = await self.tool_dispatcher(tool_name, dict(tool_input))
                # `content` is EITHER a string OR a list of typed blocks
                # (text + image). Anthropic accepts both shapes; we use
                # the list shape when the tool returned image data
                # (e.g. get_chart) so the model can SEE the chart.
                content = extract_tool_result_content(raw)
            except Exception as e:
                logger.exception("tool dispatch failed: %s", tool_name)
                # Don't raise -- let the model see the error so it can
                # recover or explain it to the user.
                content = json.dumps({"tool_error": str(e), "tool": tool_name})
                is_error = True

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        return results

    @staticmethod
    def _block_to_dict(block: Any) -> dict:
        """Normalize an Anthropic content block (pydantic) into a plain dict.

        Done so the persisted conversation is JSON-serializable. Anthropic
        accepts these dict forms on subsequent .create() calls.
        """
        if isinstance(block, dict):
            return block

        block_type = _block_field(block, "type")

        if block_type == "text":
            return {"type": "text", "text": _block_field(block, "text") or ""}

        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": _block_field(block, "id"),
                "name": _block_field(block, "name"),
                "input": _block_field(block, "input") or {},
            }

        # Fallback for forward-compat with new block types.
        try:
            return block.model_dump()  # pydantic v2
        except AttributeError:
            try:
                return block.dict()  # pydantic v1
            except AttributeError:
                return {"type": block_type or "unknown", "raw": str(block)}


def _block_field(block: Any, name: str) -> Any:
    """Read ``name`` off a content block regardless of dict-vs-pydantic shape.

    Anthropic's SDK returns pydantic objects on real API calls; tests
    feed in dicts or SimpleNamespaces; conversation persistence uses
    dicts. One helper for all three avoids the dual `getattr or .get`
    pattern that loses to SimpleNamespace (which has attributes but no
    .get).
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _safe_repr(value: Any, limit: int = 200) -> str:
    """Truncated repr for log lines."""
    s = repr(value)
    if len(s) > limit:
        return s[:limit] + "...<truncated>"
    return s
