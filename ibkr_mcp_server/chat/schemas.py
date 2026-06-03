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

MCP tool results come back as ``Sequence[TextContent | ImageContent]``.
Anthropic's ``tool_result.content`` accepts either a plain string (single
text block) OR a list of typed blocks (text + image, mixed). We adapt
here so the agent loop in agent.py doesn't have to know about MCP wire
shapes.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Union

from mcp.types import ImageContent, TextContent, Tool


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


def extract_tool_result_content(result: Any) -> Union[str, List[dict]]:
    """Coerce an MCP call_tool() return into Anthropic tool_result.content.

    Returns either a plain string (text-only tools, the common case)
    or a list of typed blocks (when the tool returned any image content,
    e.g. ``get_chart``).

    Why both shapes: Anthropic's API accepts either, and a plain string
    keeps the request body smaller for the common case. We only switch
    to the list form when the tool actually returned an image, since
    the list form requires every block to be typed explicitly.
    """
    if result is None:
        return ""

    if isinstance(result, str):
        return result

    if isinstance(result, TextContent):
        return result.text

    if isinstance(result, (list, tuple)):
        # If ANY block is an image, return the full block list -- can't
        # smuggle an image inside a plain string.
        has_image = any(
            isinstance(b, ImageContent) or
            (isinstance(b, dict) and b.get("type") == "image")
            for b in result
        )
        if has_image:
            return [_block_to_anthropic_content(b) for b in result]

        # Pure text -- concatenate for the compact shape.
        parts: List[str] = []
        for item in result:
            if isinstance(item, TextContent):
                parts.append(item.text)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif hasattr(item, "text"):
                parts.append(str(item.text))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    # Anything else: stringify. (Tests may return a dict directly.)
    try:
        import json
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def _block_to_anthropic_content(block: Any) -> dict:
    """Convert one MCP content block into Anthropic tool_result content shape."""
    if isinstance(block, TextContent):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageContent):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.mimeType or "image/png",
                "data": block.data,
            },
        }
    if isinstance(block, dict):
        return block  # assume already in the right shape
    return {"type": "text", "text": str(block)}


# Backward-compat alias. Existing callers (and several tests) import the
# old name; the new one is the canonical shape-aware version. The old
# name forces a string return, which is the right behavior for callers
# that only ever expected text.
def extract_tool_result_text(result: Any) -> str:
    """String-only variant of :func:`extract_tool_result_content`.

    If the tool produced an image, returns a JSON placeholder so the
    text-only consumers don't crash. New code should call
    ``extract_tool_result_content`` instead.
    """
    content = extract_tool_result_content(result)
    if isinstance(content, str):
        return content
    # List of blocks -- pluck just the text parts. Images become a
    # short marker so the consumer knows there was an image.
    import json
    parts: List[str] = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts) if parts else json.dumps(content, default=str)
