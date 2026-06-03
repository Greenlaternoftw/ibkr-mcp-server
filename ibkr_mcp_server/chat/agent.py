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
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


class ChatError(Exception):
    """Surfaced when the agent loop can't produce a clean reply.

    Covers: API errors from Anthropic, iteration-cap hit, malformed
    tool-use blocks. The HTTP route turns this into a 4xx/5xx JSON
    error for the browser.
    """


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
        from .schemas import extract_tool_result_text

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

    async def _run_tool_blocks(self, content_blocks) -> List[dict]:
        """Execute every tool_use block; return the matching tool_result list."""
        from .schemas import extract_tool_result_text

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
                text = extract_tool_result_text(raw)
            except Exception as e:
                logger.exception("tool dispatch failed: %s", tool_name)
                # Don't raise -- let the model see the error so it can
                # recover or explain it to the user. This is also how
                # the Anthropic tool-use spec recommends handling tool
                # failures.
                text = json.dumps({"tool_error": str(e), "tool": tool_name})
                is_error = True

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": text,
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
