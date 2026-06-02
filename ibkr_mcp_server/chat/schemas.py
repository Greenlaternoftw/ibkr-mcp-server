"""Tool schema conversion + tool-result extraction.

The MCP and Anthropic-API tool formats are 95% the same JSON Schema, with
small naming differences:

  MCP                              Anthropic
  ---                              ---------
  {                                {
    "name": ...                      "name": ...
    "description": ...               "description": ...
    "inputSchema": {...}             "input_schema": {...}
  }                                }

Tools, MCP tool-call results are returned as `Sequence[TextContent]`,
while Anthropic API expects raw text/json strings in the
`tool_result.content` slot. We adapt both directions here so the agent
loop in agent.py doesn't have to know about MCP wire shapes.
"""

from __future__ import annotations

from typing import Any, Iterable, List

from mcp.types import TextContent, Tool


def mcp_tools_to_anthropic(tools: Iterable[Tool]) -> List[dict]:
    """Convert an MCP Tool list to Anthropic's `tools` parameter shape.

    Drops nothing -- every tool in the MCP catalog is exposed to the chat
    agent. Filtering (e.g. read-only mode) belongs in a higher layer.
    """
    out: List[dict] = []
    for t in tools:
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema,
            }
        )
    return out


def extract_tool_result_text(result: Any) -> str:
    """Coerce an MCP call_tool() return into a single text string.

    The MCP server's call_tool returns ``Sequence[TextContent]``. We
    concatenate the .text fields. For non-MCP returns (mocks in tests,
    or future tools that hand back a dict directly) we fall through to
    json.dumps -- this keeps the agent loop resilient.
    """
    if result is None:
        return ""

    # Most common case: MCP TextContent list.
    if isinstance(result, list) or isinstance(result, tuple):
        parts: List[str] = []
        for item in result:
            if isinstance(item, TextContent):
                parts.append(item.text)
            elif hasattr(item, "text"):
                parts.append(str(item.text))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    # Single TextContent.
    if isinstance(result, TextContent):
        return result.text

    # Plain string -- pass through.
    if isinstance(result, str):
        return result

    # Anything else: stringify. (Tests may return a dict directly.)
    try:
        import json
        return json.dumps(result, default=str)
    except Exception:
        return str(result)
