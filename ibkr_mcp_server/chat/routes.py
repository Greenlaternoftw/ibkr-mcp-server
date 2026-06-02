"""Starlette routes for the chat wrapper.

Mounted at ``/chat`` on the existing HTTP transport (see
http_server.py). Re-uses the same BearerAuthMiddleware applied to the
whole Starlette app, so callers must present the same MCP_AUTH_TOKEN.

Endpoints:

  GET  /chat                  -> serves static/index.html
  POST /chat/api/message      -> {messages: [...]} -> {reply: ..., conversation: [...]}
  GET  /chat/api/health       -> {status, model, chat_enabled}
  GET  /chat/static/<file>    -> static assets (CSS, JS, icons)

The conversation lives on the client side (browser localStorage) for
Phase 1. The POST endpoint is stateless: caller sends the full thread
each time, server returns the updated thread. Persistence on the server
is Phase 2.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import settings
from ..tools import TOOLS, call_tool as mcp_call_tool
from .agent import AnthropicAgent, ChatError
from .prompts import SYSTEM_PROMPT
from .schemas import mcp_tools_to_anthropic

logger = logging.getLogger(__name__)


# Singleton agent per process. Built lazily so the daemon can start
# without an API key (chat is opt-in). Reset to None when settings change
# so a config reload picks up the new key/model.
_agent: Optional[AnthropicAgent] = None


def _build_agent() -> AnthropicAgent:
    """Construct the agent from current settings. May raise ChatError."""
    if not settings.chat_enabled:
        raise ChatError(
            "chat_enabled=false; set CHAT_ENABLED=true in .env to opt in"
        )
    if not settings.anthropic_api_key:
        raise ChatError(
            "ANTHROPIC_API_KEY not configured; get one at "
            "https://console.anthropic.com/ and set it in .env"
        )

    return AnthropicAgent(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        system_prompt=SYSTEM_PROMPT,
        tools_schema=mcp_tools_to_anthropic(TOOLS),
        tool_dispatcher=mcp_call_tool,
        max_iterations=settings.chat_max_iterations,
    )


def _get_agent() -> AnthropicAgent:
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


# --- handlers -------------------------------------------------------------


async def chat_index(request: Request) -> Response:
    """Serve the chat UI HTML page."""
    static_dir = Path(__file__).parent / "static"
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return JSONResponse(
            {"error": "chat UI not bundled (static/index.html missing)"},
            status_code=500,
        )
    return FileResponse(index_path)


async def chat_health(request: Request) -> Response:
    """Lightweight status endpoint so the UI can show 'chat ready' or not."""
    return JSONResponse(
        {
            "chat_enabled": settings.chat_enabled,
            "model": settings.anthropic_model if settings.chat_enabled else None,
            "has_api_key": bool(settings.anthropic_api_key),
            "tool_count": len(TOOLS),
        }
    )


async def chat_message(request: Request) -> Response:
    """Run one chat turn. Stateless -- caller sends full conversation."""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)

    messages: List[dict] = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return JSONResponse(
            {"error": "body must include a non-empty 'messages' list"},
            status_code=400,
        )

    try:
        agent = _get_agent()
    except ChatError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    try:
        result = await agent.run(messages)
    except ChatError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("unexpected error in chat turn")
        return JSONResponse({"error": f"internal error: {e}"}, status_code=500)

    return JSONResponse(
        {
            "reply_blocks": result.reply_blocks,
            "reply_text": result.reply_text(),
            "conversation": result.conversation,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "iterations": result.iterations,
            },
        }
    )


def reset_agent() -> None:
    """Test hook -- forces a fresh agent on next request (so monkey-patched
    settings get picked up)."""
    global _agent
    _agent = None


# --- route assembly -------------------------------------------------------


def chat_routes() -> List:
    """Routes to mount on the main Starlette app under /chat.

    Returns a list of Route/Mount instances. http_server.build_starlette_app
    extends the existing routes with these. Bearer auth is applied by the
    top-level middleware already.
    """
    static_dir = Path(__file__).parent / "static"

    routes = [
        Route("/chat", chat_index, methods=["GET"]),
        Route("/chat/api/health", chat_health, methods=["GET"]),
        Route("/chat/api/message", chat_message, methods=["POST"]),
    ]

    if static_dir.exists():
        routes.append(
            Mount(
                "/chat/static",
                app=StaticFiles(directory=str(static_dir)),
                name="chat-static",
            )
        )

    return routes
