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
from starlette.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..config import settings
from ..tools import TOOLS, call_tool as mcp_call_tool
from .agent import AnthropicAgent, ChatError, StreamEvent
from .persistence import ChatStore
from .prompts import SYSTEM_PROMPT
from .schemas import mcp_tools_to_anthropic

logger = logging.getLogger(__name__)


# Singleton agent per process. Built lazily so the daemon can start
# without an API key (chat is opt-in). Reset to None when settings change
# so a config reload picks up the new key/model.
_agent: Optional[AnthropicAgent] = None

# Singleton ChatStore. Lazy so the SQLite file is created on first use
# (not at import time); tests can swap in their own store path.
_store: Optional[ChatStore] = None


def _get_store() -> ChatStore:
    global _store
    if _store is None:
        from pathlib import Path
        _store = ChatStore(Path(settings.chat_db_path))
    return _store


def reset_store() -> None:
    """Test hook -- forces a fresh ChatStore on next request (so a
    monkey-patched chat_db_path is picked up)."""
    global _store
    _store = None


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


async def list_threads(request: Request) -> Response:
    """List all conversation threads, most-recent first."""
    threads = _get_store().list_threads()
    return JSONResponse({"threads": threads})


async def create_thread(request: Request) -> Response:
    """Create a new thread. Body: ``{"title": "..."}`` (title optional)."""
    try:
        body = await request.json() if await request.body() else {}
    except json.JSONDecodeError:
        body = {}
    title = (body.get("title") or "").strip() or "New chat"
    thread = _get_store().create_thread(title)
    return JSONResponse(thread, status_code=201)


async def get_thread_messages(request: Request) -> Response:
    """Get the message list for a thread. Used on thread-switch to
    populate the UI from server state."""
    thread_id = request.path_params["thread_id"]
    store = _get_store()
    thread = store.get_thread(thread_id)
    if thread is None:
        return JSONResponse({"error": "thread not found"}, status_code=404)
    messages = store.get_messages(thread_id)
    return JSONResponse({"thread": thread, "messages": messages})


async def rename_thread(request: Request) -> Response:
    """Rename a thread. Body: ``{"title": "..."}``."""
    thread_id = request.path_params["thread_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
    new_title = (body.get("title") or "").strip()
    if not new_title:
        return JSONResponse({"error": "title required"}, status_code=400)
    if not _get_store().rename_thread(thread_id, new_title):
        return JSONResponse({"error": "thread not found"}, status_code=404)
    return JSONResponse({"ok": True, "id": thread_id, "title": new_title})


async def delete_thread(request: Request) -> Response:
    """Hard-delete a thread and all its messages."""
    thread_id = request.path_params["thread_id"]
    if not _get_store().delete_thread(thread_id):
        return JSONResponse({"error": "thread not found"}, status_code=404)
    return Response(status_code=204)


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


async def chat_message_stream(request: Request) -> Response:
    """Stream one chat turn over SSE.

    Body: ``{"messages": [...], "thread_id": "thr_..." (optional)}``.

    Response is ``text/event-stream``; each event is ``data: <json>\\n\\n``
    where the JSON has a ``type`` field (``text`` / ``tool_call`` /
    ``tool_result`` / ``done`` / ``error``).

    When ``thread_id`` is supplied, the final conversation (including
    any tool_use / tool_result blocks the agent added) is persisted to
    the SQLite store atomically after the turn completes -- the client
    no longer has to be the sole source of truth.

    Client-side: consume via ``fetch`` + ``ReadableStream`` (browsers
    don't let ``EventSource`` send POST bodies, so we don't use it).
    See ``static/index.html`` for the consumer pattern.
    """
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

    thread_id = body.get("thread_id")
    if thread_id is not None:
        # If a thread_id was supplied, the thread must exist before we
        # accept the turn. Otherwise a successful save here would create
        # a phantom orphan and the next list_threads call would surface
        # it confusingly.
        store = _get_store()
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                {"error": f"thread not found: {thread_id}"},
                status_code=404,
            )

    try:
        agent = _get_agent()
    except ChatError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    async def event_source():
        # Outer try is the last line of defence -- if the agent crashes
        # before yielding anything, the client still gets an error event
        # instead of an opaque connection close.
        try:
            async for event in agent.run_stream(messages):
                # Snapshot the conversation on `done` so we can persist
                # the canonical post-turn state (includes any tool_use
                # / tool_result blocks the agent appended).
                if event.type == "done" and thread_id:
                    try:
                        _get_store().replace_messages(
                            thread_id, event.payload.get("conversation") or []
                        )
                    except Exception:
                        logger.exception(
                            "failed to persist chat thread %s", thread_id
                        )
                yield event.to_sse()
        except Exception as e:
            logger.exception("unexpected error in chat stream")
            yield StreamEvent("error", {"message": f"internal: {e}"}).to_sse()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # No buffering -- proxies (nginx, Cloudflare) sometimes hold
            # partial SSE responses for a few seconds otherwise.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            # Most proxies set this; harmless if redundant.
            "Connection": "keep-alive",
        },
    )


async def chat_message(request: Request) -> Response:
    """Run one chat turn (non-streaming).

    Body: ``{"messages": [...], "thread_id": "thr_..." (optional)}``.
    Same persistence semantics as the streaming endpoint: pass thread_id
    to have the canonical post-turn conversation saved server-side.
    """
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

    thread_id = body.get("thread_id")
    if thread_id is not None:
        store = _get_store()
        if store.get_thread(thread_id) is None:
            return JSONResponse(
                {"error": f"thread not found: {thread_id}"},
                status_code=404,
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

    # Persist the canonical post-turn state. We swallow persistence
    # failures into the log -- the user already has a valid response;
    # losing a save is recoverable on the next turn.
    if thread_id:
        try:
            _get_store().replace_messages(thread_id, result.conversation)
        except Exception:
            logger.exception("failed to persist chat thread %s", thread_id)

    return JSONResponse(
        {
            "reply_blocks": result.reply_blocks,
            "reply_text": result.reply_text(),
            "conversation": result.conversation,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_creation_input_tokens": result.cache_creation_input_tokens,
                "cache_read_input_tokens": result.cache_read_input_tokens,
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
        Route(
            "/chat/api/message/stream",
            chat_message_stream,
            methods=["POST"],
        ),
        # Thread CRUD for server-side conversation persistence.
        Route("/chat/api/threads", list_threads, methods=["GET"]),
        Route("/chat/api/threads", create_thread, methods=["POST"]),
        Route(
            "/chat/api/threads/{thread_id}",
            get_thread_messages,
            methods=["GET"],
        ),
        Route(
            "/chat/api/threads/{thread_id}",
            rename_thread,
            methods=["PATCH"],
        ),
        Route(
            "/chat/api/threads/{thread_id}",
            delete_thread,
            methods=["DELETE"],
        ),
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
