"""Layer 7 — in-house chat wrapper.

A small web app served on the same HTTP transport as the MCP endpoint.
Calls Anthropic API directly with our own system prompt so the
consumer-product safety overlay (which refuses to invoke destructive
trading tools) is bypassed.

Architecture:

    Browser  (over Tailscale + bearer auth)
       |
       v
    /chat                  -- HTML page
    /chat/api/message      -- POST: user msg -> agent loop -> reply
       |
       v
    AnthropicAgent
       |  (Anthropic API w/ tools schema)
       v
    Tool execution -> existing IBKRClient methods
       |
       v
    JSON result -> Anthropic API -> reply

The agent loop terminates on either `stop_reason == "end_turn"` or
when `chat_max_iterations` iterations have run (whichever first), so a
runaway tool-call cycle can't burn budget unbounded.
"""

from .agent import AnthropicAgent, ChatError
from .prompts import SYSTEM_PROMPT
from .schemas import mcp_tools_to_anthropic, extract_tool_result_text

__all__ = [
    "AnthropicAgent",
    "ChatError",
    "SYSTEM_PROMPT",
    "mcp_tools_to_anthropic",
    "extract_tool_result_text",
]
