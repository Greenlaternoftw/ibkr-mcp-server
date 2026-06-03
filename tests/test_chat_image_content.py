"""Tests for image-bearing tool results in the chat agent.

The chart tools (``get_chart``, etc.) return mixed text + image content
from the MCP dispatcher. These tests verify that:

  * ``extract_tool_result_content`` returns a string for text-only
    results (the cheap path Anthropic prefers) and a typed-block list
    when ANY image is present.
  * The agent's tool_result event for image content includes the
    base64 PNG payload (so the SSE frontend can render it inline) AND
    the text summary as preview.
  * Anthropic API tool_result.content gets the typed block list verbatim
    when images are involved, so the model can see the chart.

Matplotlib is NOT exercised here -- the chart module has its own (lazy)
import path. These tests use byte-string PNG stand-ins.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import ImageContent, TextContent

from ibkr_mcp_server.chat.schemas import (
    extract_tool_result_content,
    extract_tool_result_text,
)


# A 1x1 transparent PNG -- 70 bytes, valid PNG, fine as a stand-in.
_TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)
_TINY_PNG_B64 = base64.b64encode(_TINY_PNG).decode("ascii")


# --- content extraction ---------------------------------------------------


class TestExtractContent:
    def test_text_only_returns_string(self):
        """Common case: text-only tools return a plain string.
        Anthropic accepts this in tool_result.content as the compact form."""
        result = [TextContent(type="text", text='{"a": 1}')]
        out = extract_tool_result_content(result)
        assert out == '{"a": 1}'
        assert isinstance(out, str)

    def test_text_only_multiple_blocks_joined(self):
        result = [
            TextContent(type="text", text="line one"),
            TextContent(type="text", text="line two"),
        ]
        out = extract_tool_result_content(result)
        assert out == "line one\nline two"

    def test_with_image_returns_block_list(self):
        """When an image is present, we must return the typed-block list
        (the string form can't carry images)."""
        result = [
            TextContent(type="text", text="here's the chart"),
            ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png"),
        ]
        out = extract_tool_result_content(result)
        assert isinstance(out, list)
        assert len(out) == 2
        assert out[0] == {"type": "text", "text": "here's the chart"}
        assert out[1]["type"] == "image"
        # Anthropic's nested source format
        assert out[1]["source"]["type"] == "base64"
        assert out[1]["source"]["media_type"] == "image/png"
        assert out[1]["source"]["data"] == _TINY_PNG_B64

    def test_image_only_returns_block_list(self):
        result = [ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png")]
        out = extract_tool_result_content(result)
        assert isinstance(out, list)
        assert out[0]["type"] == "image"

    def test_legacy_text_extractor_still_works(self):
        """Existing string-only callers must not regress when given
        image content -- they get a placeholder instead of crashing."""
        result = [
            TextContent(type="text", text="summary"),
            ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png"),
        ]
        out = extract_tool_result_text(result)
        assert "summary" in out
        assert "[image]" in out


# --- streaming event payload ----------------------------------------------


def _mk_text_block(text: str):
    from types import SimpleNamespace
    return SimpleNamespace(type="text", text=text)


def _mk_tool_use_block(tu_id: str, name: str, args: dict):
    from types import SimpleNamespace
    return SimpleNamespace(type="tool_use", id=tu_id, name=name, input=args)


def _mk_response(content_blocks, stop_reason: str,
                 in_tok=100, out_tok=50, cache_create=0, cache_read=0):
    from types import SimpleNamespace
    return SimpleNamespace(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=in_tok, output_tokens=out_tok,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
        ),
    )


def _make_agent(dispatcher):
    """Build a chat agent with a mocked Anthropic client."""
    from ibkr_mcp_server.chat.agent import AnthropicAgent
    with patch("anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value = MagicMock()
        agent = AnthropicAgent(
            api_key="sk-test", model="claude-test",
            system_prompt="(test)",
            tools_schema=[{"name": "get_chart", "description": "", "input_schema": {"type": "object"}}],
            tool_dispatcher=dispatcher,
            max_iterations=5,
        )
    return agent, MockClient.return_value


class _FakeStream:
    def __init__(self, text_chunks, final_msg):
        self._chunks = text_chunks
        self._final = final_msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    @property
    def text_stream(self):
        chunks = self._chunks

        async def gen():
            for c in chunks:
                yield c
        return gen()

    async def get_final_message(self):
        return self._final


class TestStreamingImageEvent:
    @pytest.mark.asyncio
    async def test_chart_tool_emits_image_b64_in_tool_result_event(self):
        """Most important new behavior: when a chart tool returns image
        content, the SSE tool_result event must include image_b64 +
        image_mime so the browser can render `<img src="data:...">`."""
        # Dispatcher mimics the MCP dispatcher: returns a list with
        # TextContent + ImageContent.
        async def chart_dispatcher(name, args):
            return [
                TextContent(type="text", text='{"symbol":"AAPL","summary":"AAPL chart"}'),
                ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png"),
            ]

        agent, sdk = _make_agent(chart_dispatcher)

        # Iter 1: model calls get_chart. Iter 2: wraps up.
        sdk.messages.stream = MagicMock(side_effect=[
            _FakeStream(
                [],
                _mk_response(
                    [_mk_tool_use_block("tu_chart", "get_chart", {"symbol": "AAPL"})],
                    stop_reason="tool_use",
                ),
            ),
            _FakeStream(
                ["Here's "],
                _mk_response(
                    [_mk_text_block("Here's the chart.")],
                    stop_reason="end_turn",
                ),
            ),
        ])

        events = []
        async for ev in agent.run_stream([{"role": "user", "content": "show me AAPL"}]):
            events.append(ev)

        # Find the tool_result event.
        tool_results = [e for e in events if e.type == "tool_result"]
        assert len(tool_results) == 1
        payload = tool_results[0].payload

        # The browser uses these two fields to render the image inline.
        assert "image_b64" in payload
        assert payload["image_b64"] == _TINY_PNG_B64
        assert payload["image_mime"] == "image/png"
        # Preview text is also extracted for display alongside the image.
        assert "AAPL" in payload["preview"]
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_text_only_tool_does_not_emit_image_field(self):
        """Regression: non-chart tools must keep working without image fields."""
        async def text_dispatcher(name, args):
            return [TextContent(type="text", text='{"connected": true}')]

        agent, sdk = _make_agent(text_dispatcher)
        sdk.messages.stream = MagicMock(side_effect=[
            _FakeStream(
                [],
                _mk_response(
                    [_mk_tool_use_block("tu_1", "get_connection_status", {})],
                    stop_reason="tool_use",
                ),
            ),
            _FakeStream(
                ["OK"],
                _mk_response([_mk_text_block("OK")], stop_reason="end_turn"),
            ),
        ])

        events = []
        async for ev in agent.run_stream([{"role": "user", "content": "check"}]):
            events.append(ev)

        tool_results = [e for e in events if e.type == "tool_result"]
        assert "image_b64" not in tool_results[0].payload
        assert "image_mime" not in tool_results[0].payload
        # Preview still populated for the text content.
        assert "connected" in tool_results[0].payload["preview"]


# --- conversation persistence with image content --------------------------


class TestConversationWithImages:
    """Ensure tool_result content with images survives a round-trip through
    the conversation, so the model can SEE the chart on the next turn."""

    @pytest.mark.asyncio
    async def test_image_content_appended_to_conversation(self):
        async def chart_dispatcher(name, args):
            return [
                TextContent(type="text", text='{"summary":"chart text"}'),
                ImageContent(type="image", data=_TINY_PNG_B64, mimeType="image/png"),
            ]

        agent, sdk = _make_agent(chart_dispatcher)
        sdk.messages.stream = MagicMock(side_effect=[
            _FakeStream(
                [],
                _mk_response(
                    [_mk_tool_use_block("tu_chart", "get_chart", {"symbol": "AAPL"})],
                    stop_reason="tool_use",
                ),
            ),
            _FakeStream(
                ["Got it."],
                _mk_response([_mk_text_block("Got it.")], stop_reason="end_turn"),
            ),
        ])

        events = []
        async for ev in agent.run_stream([{"role": "user", "content": "chart"}]):
            events.append(ev)

        done = next(e for e in events if e.type == "done")
        convo = done.payload["conversation"]
        # Find the user turn that holds the tool_result.
        tool_result_msgs = [
            m for m in convo
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and m["content"]
            and isinstance(m["content"][0], dict)
            and m["content"][0].get("type") == "tool_result"
        ]
        assert len(tool_result_msgs) == 1
        result_block = tool_result_msgs[0]["content"][0]
        # The content is the typed-block list, including the image.
        assert isinstance(result_block["content"], list)
        image_blocks = [
            b for b in result_block["content"]
            if isinstance(b, dict) and b.get("type") == "image"
        ]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["data"] == _TINY_PNG_B64
