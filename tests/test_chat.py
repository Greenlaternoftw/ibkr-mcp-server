"""Tests for the in-house chat wrapper (Layer 7).

Covers the three things that need to be right for production:

  1. Tool schema conversion: every MCP tool becomes a valid Anthropic
     tool definition. (If we silently drop a tool the chat agent will
     refuse to call it.)

  2. Agent loop control flow: end_turn -> return; tool_use -> dispatch
     and loop; iteration cap raises ChatError.

  3. Confirmation-gate flow: when a tool returns needs_confirmation, the
     model can be prompted (in a follow-up turn) to call again with
     confirm=true. This is the path that actually fixes the refusal
     problem we built this for, so it gets a dedicated end-to-end test
     with a mocked Anthropic API.

We mock the Anthropic SDK so these tests run with no API key and no
network. The chat HTTP routes are tested in test_http_server.py via the
Starlette test client.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ibkr_mcp_server.chat.agent import AgentResult, AnthropicAgent, ChatError
from ibkr_mcp_server.chat.schemas import (
    extract_tool_result_text,
    mcp_tools_to_anthropic,
)


# --- schema conversion ----------------------------------------------------


class TestSchemaConversion:
    def test_every_mcp_tool_maps_to_anthropic_shape(self):
        """Each tool ends up with name + description + input_schema."""
        from ibkr_mcp_server.tools import TOOLS

        converted = mcp_tools_to_anthropic(TOOLS)
        assert len(converted) == len(TOOLS)
        for t in converted:
            assert isinstance(t["name"], str) and t["name"]
            assert isinstance(t["description"], str)
            assert isinstance(t["input_schema"], dict)
            # Anthropic's tools spec requires input_schema to be an
            # object schema, not (say) a string.
            assert t["input_schema"].get("type") == "object"

    def test_destructive_tools_carry_confirm_param(self):
        """Sanity check that the confirmation-gate parameter survived the
        conversion. If it didn't, the chat agent couldn't pass confirm=true."""
        from ibkr_mcp_server.tools import TOOLS

        gated = {
            "place_order",
            "place_oca_group",
            "stop_swing_strategy",
            "stop_reversal_entry",
            "update_swing_params",
        }
        converted = mcp_tools_to_anthropic(TOOLS)
        by_name = {t["name"]: t for t in converted}
        for n in gated:
            assert n in by_name, f"missing destructive tool: {n}"
            props = by_name[n]["input_schema"].get("properties", {})
            assert "confirm" in props, f"{n} missing confirm parameter"


# --- result extraction ----------------------------------------------------


class TestExtractToolResult:
    def test_text_content_list(self):
        from mcp.types import TextContent

        result = [TextContent(type="text", text='{"a": 1}')]
        assert extract_tool_result_text(result) == '{"a": 1}'

    def test_multiple_text_blocks_joined(self):
        from mcp.types import TextContent

        result = [
            TextContent(type="text", text="line one"),
            TextContent(type="text", text="line two"),
        ]
        assert extract_tool_result_text(result) == "line one\nline two"

    def test_dict_falls_back_to_json(self):
        out = extract_tool_result_text({"status": "ok", "n": 3})
        assert json.loads(out) == {"status": "ok", "n": 3}

    def test_none_returns_empty(self):
        assert extract_tool_result_text(None) == ""


# --- agent loop -----------------------------------------------------------


def _mk_text_block(text: str):
    """Build a mock Anthropic content block of type='text'."""
    return SimpleNamespace(type="text", text=text)


def _mk_tool_use_block(tool_use_id: str, name: str, tool_input: dict):
    """Build a mock Anthropic content block of type='tool_use'."""
    return SimpleNamespace(
        type="tool_use", id=tool_use_id, name=name, input=tool_input
    )


def _mk_response(
    content_blocks,
    stop_reason: str,
    in_tok: int = 100,
    out_tok: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
):
    """Build a mock anthropic messages.create() response.

    cache_creation/cache_read default to 0 so existing tests don't have
    to care about caching; the caching-specific test below sets them.
    """
    return SimpleNamespace(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        ),
    )


def _make_agent(tool_dispatcher, *, max_iterations: int = 12):
    """Build an AnthropicAgent with a mocked SDK client.

    We patch anthropic.AsyncAnthropic before constructing the agent so the
    real SDK never tries to talk to a real API key.
    """
    with patch("anthropic.AsyncAnthropic") as MockClient:
        instance = MagicMock()
        MockClient.return_value = instance
        agent = AnthropicAgent(
            api_key="sk-test",
            model="claude-sonnet-test",
            system_prompt="(test prompt)",
            tools_schema=[
                {"name": "get_portfolio", "description": "x", "input_schema": {"type": "object", "properties": {}}},
                {"name": "place_order", "description": "x", "input_schema": {"type": "object", "properties": {}}},
            ],
            tool_dispatcher=tool_dispatcher,
            max_iterations=max_iterations,
        )
    return agent, instance


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_end_turn_returns_reply_immediately(self):
        """No tool_use -> one Anthropic call, return the assistant text."""
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            return_value=_mk_response([_mk_text_block("Hi there.")], stop_reason="end_turn")
        )

        result = await agent.run([{"role": "user", "content": "Hello"}])

        assert result.iterations == 1
        assert result.reply_text() == "Hi there."
        assert dispatcher.await_count == 0
        # Conversation grew by one assistant turn.
        assert result.conversation[-1]["role"] == "assistant"
        assert result.conversation[-1]["content"][0]["text"] == "Hi there."

    @pytest.mark.asyncio
    async def test_tool_use_dispatches_then_continues(self):
        """tool_use stop -> dispatcher called -> loop -> end_turn."""
        dispatcher = AsyncMock(return_value='{"connected": true}')
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            side_effect=[
                # First call: model issues a tool_use.
                _mk_response(
                    [_mk_tool_use_block("tu_1", "get_connection_status", {})],
                    stop_reason="tool_use",
                ),
                # Second call: model wraps up.
                _mk_response(
                    [_mk_text_block("You're connected to IBKR.")],
                    stop_reason="end_turn",
                ),
            ]
        )

        result = await agent.run([{"role": "user", "content": "Are you connected?"}])

        assert result.iterations == 2
        assert dispatcher.await_count == 1
        dispatcher.assert_awaited_with("get_connection_status", {})
        assert result.reply_text() == "You're connected to IBKR."

        # The conversation now has: user, assistant(tool_use), user(tool_result), assistant(text).
        roles = [m["role"] for m in result.conversation]
        assert roles == ["user", "assistant", "user", "assistant"]

        # The tool_result block must reference the tool_use_id.
        tool_result_block = result.conversation[2]["content"][0]
        assert tool_result_block["type"] == "tool_result"
        assert tool_result_block["tool_use_id"] == "tu_1"
        assert tool_result_block["content"] == '{"connected": true}'
        assert tool_result_block["is_error"] is False

    @pytest.mark.asyncio
    async def test_dispatcher_exception_is_surfaced_to_model(self):
        """Tool failures are non-fatal: the loop continues with an error
        tool_result so the model can apologize / retry."""
        dispatcher = AsyncMock(side_effect=RuntimeError("simulated"))
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            side_effect=[
                _mk_response(
                    [_mk_tool_use_block("tu_x", "get_portfolio", {})],
                    stop_reason="tool_use",
                ),
                _mk_response(
                    [_mk_text_block("Sorry, that tool errored.")],
                    stop_reason="end_turn",
                ),
            ]
        )

        result = await agent.run([{"role": "user", "content": "portfolio?"}])

        # Loop kept going.
        assert result.iterations == 2
        # The tool_result block carries is_error=True so the model knows.
        tool_result_block = result.conversation[2]["content"][0]
        assert tool_result_block["is_error"] is True
        assert "simulated" in tool_result_block["content"]

    @pytest.mark.asyncio
    async def test_iteration_cap_raises_chat_error(self):
        """A model stuck in a tool-call loop must NOT burn forever."""
        dispatcher = AsyncMock(return_value="{}")
        agent, sdk = _make_agent(dispatcher, max_iterations=3)
        # Always return tool_use; the loop should bail after 3 iterations.
        sdk.messages.create = AsyncMock(
            return_value=_mk_response(
                [_mk_tool_use_block("tu_loop", "get_portfolio", {})],
                stop_reason="tool_use",
            )
        )

        with pytest.raises(ChatError) as exc_info:
            await agent.run([{"role": "user", "content": "loop"}])
        assert "iteration cap" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_api_error_becomes_chat_error(self):
        """Anthropic SDK exceptions become a typed ChatError."""
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(side_effect=RuntimeError("503"))

        with pytest.raises(ChatError) as exc_info:
            await agent.run([{"role": "user", "content": "hi"}])
        assert "Anthropic API call failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_token_usage_accumulates_across_iterations(self):
        """Token counts add up over a multi-step tool-call turn."""
        dispatcher = AsyncMock(return_value='{"ok": true}')
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            side_effect=[
                _mk_response(
                    [_mk_tool_use_block("tu", "get_portfolio", {})],
                    stop_reason="tool_use",
                    in_tok=80, out_tok=40,
                ),
                _mk_response(
                    [_mk_text_block("Done.")],
                    stop_reason="end_turn",
                    in_tok=120, out_tok=20,
                ),
            ]
        )

        result = await agent.run([{"role": "user", "content": "go"}])
        assert result.input_tokens == 200
        assert result.output_tokens == 60


# --- prompt caching -------------------------------------------------------
#
# The agent loop wraps system_prompt in a one-element list carrying
# `cache_control: {"type": "ephemeral"}` so the ~9.5K-token tools+system
# prefix is cached across turns. These tests verify:
#   1. The marker is actually placed in the request (we inspect what got
#      passed to client.messages.create).
#   2. cache_creation_input_tokens / cache_read_input_tokens flow into
#      AgentResult so the operator can observe cache effectiveness.
# If either of these silently regress, token costs balloon ~5-10x.


class TestPromptCaching:
    @pytest.mark.asyncio
    async def test_system_block_carries_cache_control(self):
        """The request must include a system list with cache_control on
        the last (and only) block. Render order is tools -> system ->
        messages, so this marker caches BOTH tools and the system prompt.
        """
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            return_value=_mk_response([_mk_text_block("ok")], stop_reason="end_turn")
        )

        await agent.run([{"role": "user", "content": "hi"}])

        # Inspect the kwargs passed to messages.create.
        assert sdk.messages.create.await_count == 1
        kwargs = sdk.messages.create.await_args.kwargs
        system = kwargs.get("system")

        # Must be a list, not a string -- string form can't carry cache_control.
        assert isinstance(system, list), f"system must be a list for caching, got {type(system)}"
        assert len(system) >= 1

        # Marker on the last block caches everything up to it (tools + system).
        last = system[-1]
        assert last.get("type") == "text"
        assert last.get("cache_control") == {"type": "ephemeral"}
        # Sanity: the actual prompt text is preserved.
        assert last.get("text") == "(test prompt)"

    @pytest.mark.asyncio
    async def test_cache_token_counts_accumulate(self):
        """cache_creation and cache_read tokens roll up across iterations
        into AgentResult. First iter writes the cache, second reads it --
        AgentResult must reflect the sum, not just the last response."""
        dispatcher = AsyncMock(return_value='{"ok": true}')
        agent, sdk = _make_agent(dispatcher)
        sdk.messages.create = AsyncMock(
            side_effect=[
                # Iteration 1: write the cache. cache_read=0 means miss.
                _mk_response(
                    [_mk_tool_use_block("tu1", "get_portfolio", {})],
                    stop_reason="tool_use",
                    in_tok=80, out_tok=40,
                    cache_creation=9500, cache_read=0,
                ),
                # Iteration 2: cache hit. cache_creation=0, cache_read=9500.
                _mk_response(
                    [_mk_text_block("Done.")],
                    stop_reason="end_turn",
                    in_tok=120, out_tok=20,
                    cache_creation=0, cache_read=9500,
                ),
            ]
        )

        result = await agent.run([{"role": "user", "content": "go"}])

        assert result.cache_creation_input_tokens == 9500
        assert result.cache_read_input_tokens == 9500
        # Sanity: regular token counts still work alongside cache fields.
        assert result.input_tokens == 200
        assert result.output_tokens == 60

    @pytest.mark.asyncio
    async def test_missing_cache_usage_fields_are_zero(self):
        """If Anthropic ever returns a usage object WITHOUT the cache
        fields (older SDK, error path, etc.), the agent must not crash --
        cache counters stay at 0."""
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)
        # Build a response whose usage object lacks the cache_* attrs.
        sdk.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[_mk_text_block("ok")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=50, output_tokens=10),
            )
        )

        result = await agent.run([{"role": "user", "content": "hi"}])
        assert result.cache_creation_input_tokens == 0
        assert result.cache_read_input_tokens == 0


# --- SSE streaming --------------------------------------------------------
#
# The agent's run_stream() method yields StreamEvent objects in order:
#   text deltas during the model's response -> tool_call after each
#   tool_use block -> tool_result after our dispatcher returns -> done
#   on end_turn (or error on failure). The HTTP layer converts each
#   event into a "data: <json>\n\n" SSE line.
#
# Tests below verify (a) the event-order contract holds in the happy
# path, (b) tool_use stop triggers tool dispatch + tool_result events,
# (c) the SSE wire format is correct.


class _FakeStream:
    """Minimal async-context-manager that mimics anthropic's stream API.

    Real SDK exposes ``async for chunk in stream.text_stream`` and
    ``await stream.get_final_message()``. We replicate both so the agent's
    run_stream() can be exercised without a real Anthropic client.
    """

    def __init__(self, text_chunks: List[str], final_message: Any):
        self._text_chunks = text_chunks
        self._final = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        chunks = self._text_chunks

        async def gen():
            for c in chunks:
                yield c

        return gen()

    async def get_final_message(self):
        return self._final


def _mk_final_message(content_blocks, stop_reason: str, in_tok=100, out_tok=50,
                     cache_create=0, cache_read=0):
    """Builds the object returned by get_final_message() on a fake stream."""
    return SimpleNamespace(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=in_tok, output_tokens=out_tok,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
        ),
    )


class TestStreamEventWireFormat:
    def test_to_sse_is_well_formed(self):
        from ibkr_mcp_server.chat.agent import StreamEvent
        ev = StreamEvent("text", {"delta": "hello"})
        line = ev.to_sse()
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        # The line between "data: " and "\n\n" must be valid JSON.
        body = line[len("data: "): -2]
        parsed = json.loads(body)
        assert parsed == {"type": "text", "delta": "hello"}

    def test_to_sse_handles_nested_payload(self):
        from ibkr_mcp_server.chat.agent import StreamEvent
        ev = StreamEvent("done", {"conversation": [{"role": "user", "content": "hi"}]})
        line = ev.to_sse()
        body = json.loads(line[len("data: "): -2])
        assert body["type"] == "done"
        assert body["conversation"][0]["role"] == "user"


class TestStreamAgentLoop:
    @pytest.mark.asyncio
    async def test_end_turn_yields_text_then_done(self):
        """Simplest path: model streams 'Hi there.' as 2 chunks, stops on
        end_turn. We expect: text, text, done (in that order)."""
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)
        final = _mk_final_message(
            [_mk_text_block("Hi there.")],
            stop_reason="end_turn",
            cache_read=4330,
        )
        sdk.messages.stream = MagicMock(
            return_value=_FakeStream(["Hi ", "there."], final)
        )

        events = []
        async for ev in agent.run_stream([{"role": "user", "content": "Hello"}]):
            events.append(ev)

        types = [e.type for e in events]
        assert types == ["text", "text", "done"]
        assert events[0].payload == {"delta": "Hi "}
        assert events[1].payload == {"delta": "there."}

        done = events[-1]
        assert done.payload["usage"]["cache_read_input_tokens"] == 4330
        assert done.payload["usage"]["iterations"] == 1
        # Conversation grew by one assistant turn with the text block.
        convo = done.payload["conversation"]
        assert convo[-1]["role"] == "assistant"
        assert convo[-1]["content"][0]["text"] == "Hi there."

    @pytest.mark.asyncio
    async def test_tool_use_then_end_turn_yields_full_sequence(self):
        """Realistic two-iteration flow:
          iter 1: model emits tool_use; stream completes; we fire
                  tool_call event, dispatch, fire tool_result event.
          iter 2: model emits final text; stream completes; done.
        """
        dispatcher = AsyncMock(return_value='{"connected": true}')
        agent, sdk = _make_agent(dispatcher)

        # Iteration 1: tool_use stop. No text streamed (tool calls
        # generally don't have a preceding text block).
        final1 = _mk_final_message(
            [_mk_tool_use_block("tu_1", "get_connection_status", {})],
            stop_reason="tool_use",
        )
        # Iteration 2: short text, end_turn.
        final2 = _mk_final_message(
            [_mk_text_block("Yes, connected.")],
            stop_reason="end_turn",
        )

        # SDK's stream() is called twice -- once per iteration.
        sdk.messages.stream = MagicMock(
            side_effect=[
                _FakeStream([], final1),
                _FakeStream(["Yes, ", "connected."], final2),
            ]
        )

        events = []
        async for ev in agent.run_stream(
            [{"role": "user", "content": "are you connected?"}]
        ):
            events.append(ev)

        types = [e.type for e in events]
        # Expected: tool_call (after iter 1), tool_result (after dispatch),
        # text x2 (during iter 2), done.
        assert types == ["tool_call", "tool_result", "text", "text", "done"]

        # tool_call carries the name + parsed input
        tc = next(e for e in events if e.type == "tool_call")
        assert tc.payload["name"] == "get_connection_status"

        # tool_result carries ok + truncated preview
        tr = next(e for e in events if e.type == "tool_result")
        assert tr.payload["ok"] is True
        assert "connected" in tr.payload["preview"]

        # Dispatcher was called exactly once
        assert dispatcher.await_count == 1

    @pytest.mark.asyncio
    async def test_api_failure_emits_error_event(self):
        """If the SDK raises (network error, rate limit, etc.), we
        emit an error StreamEvent rather than letting the exception
        propagate. The browser renders it as an inline error bubble."""
        dispatcher = AsyncMock()
        agent, sdk = _make_agent(dispatcher)

        # Use a stream context manager that raises in __aenter__.
        class _RaisingStream:
            async def __aenter__(self):
                raise RuntimeError("simulated 503")

            async def __aexit__(self, *_):
                return False

        sdk.messages.stream = MagicMock(return_value=_RaisingStream())

        events = []
        async for ev in agent.run_stream([{"role": "user", "content": "go"}]):
            events.append(ev)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "simulated 503" in events[0].payload["message"]


# --- end-to-end confirmation-gate flow ------------------------------------


class TestConfirmationGateFlow:
    """The flow the system prompt is designed to produce.

    Turn 1: user says "buy 1 AAPL", model calls place_order with confirm
            implicit/false. Tool returns needs_confirmation. Model
            surfaces preview and asks "confirm?".
    Turn 2: user says "yes". Model calls place_order again with
            confirm=true. Tool returns submitted.

    We verify both the dispatch arguments AND that the model gets to see
    the daemon's preview, because that's the whole point of the system.
    """

    @pytest.mark.asyncio
    async def test_two_step_buy_then_confirm(self):
        # The dispatcher emulates the daemon's confirmation gate: first
        # call (no confirm) returns needs_confirmation; second call (with
        # confirm=true) returns submitted.
        calls: List[tuple] = []

        async def dispatcher(name, args):
            calls.append((name, dict(args)))
            if name == "place_order" and not args.get("confirm"):
                return json.dumps({
                    "status": "needs_confirmation",
                    "action": "place_order",
                    "preview": {"symbol": "AAPL", "quantity": 1, "order_type": "MKT"},
                    "message": "Pass confirm=true to actually execute.",
                })
            if name == "place_order" and args.get("confirm"):
                return json.dumps({"status": "submitted", "order_id": 7777})
            return json.dumps({"status": "unknown"})

        agent, sdk = _make_agent(dispatcher)

        # --- turn 1: buy request ---
        sdk.messages.create = AsyncMock(
            side_effect=[
                # Model decides to call place_order (no confirm).
                _mk_response(
                    [_mk_tool_use_block(
                        "tu_a",
                        "place_order",
                        {"symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"},
                    )],
                    stop_reason="tool_use",
                ),
                # After seeing the needs_confirmation result, model asks user.
                _mk_response(
                    [_mk_text_block(
                        "Preview: BUY 1 AAPL MKT. The daemon needs explicit "
                        "confirmation. Confirm?"
                    )],
                    stop_reason="end_turn",
                ),
            ]
        )

        result1 = await agent.run(
            [{"role": "user", "content": "Buy 1 share of AAPL at market"}]
        )
        assert "Confirm?" in result1.reply_text()
        # First call did NOT pass confirm=true.
        assert calls[0] == ("place_order", {
            "symbol": "AAPL", "action": "BUY", "quantity": 1, "order_type": "MKT"
        })

        # --- turn 2: user says yes ---
        # The browser would append a user message and resend the whole convo.
        followup_convo = result1.conversation + [
            {"role": "user", "content": "yes confirm"}
        ]
        sdk.messages.create = AsyncMock(
            side_effect=[
                # Model calls place_order again, this time with confirm=true.
                _mk_response(
                    [_mk_tool_use_block(
                        "tu_b",
                        "place_order",
                        {"symbol": "AAPL", "action": "BUY", "quantity": 1,
                         "order_type": "MKT", "confirm": True},
                    )],
                    stop_reason="tool_use",
                ),
                _mk_response(
                    [_mk_text_block("Submitted. Order ID 7777.")],
                    stop_reason="end_turn",
                ),
            ]
        )

        result2 = await agent.run(followup_convo)
        assert "Submitted" in result2.reply_text()
        # The second place_order call carried confirm=True -- this is the
        # exact behavior the system prompt is teaching the model to do.
        assert calls[1][0] == "place_order"
        assert calls[1][1].get("confirm") is True
