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


def _mk_response(content_blocks, stop_reason: str, in_tok: int = 100, out_tok: int = 50):
    """Build a mock anthropic messages.create() response."""
    return SimpleNamespace(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
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
