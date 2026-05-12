"""Layer 5b — HTTP transport for the MCP server.

Mounts the MCP protocol on a Starlette ASGI app over `streamable_http`, plus
an unauthenticated `/healthz` endpoint for monitoring.

Bearer auth via `MCP_AUTH_TOKEN`:
  - All routes except `/healthz` require `Authorization: Bearer <token>`
    matching `MCP_AUTH_TOKEN` whenever the token is configured.
  - Required (enforced at startup) when binding to anything other than
    127.0.0.1 — refuses to start without a token in that case.
  - Optional on a localhost bind (the kernel-level filter is your defense
    of last resort there); developers can skip it for local poking.
"""

from __future__ import annotations

import logging
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .client import ibkr_client
from .tools import server


logger = logging.getLogger(__name__)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Verify `Authorization: Bearer <token>` matches the configured token.

    - `/healthz` is always allowed (monitoring, watchdog probes).
    - When `expected_token` is None, every other route is also allowed
      (localhost-only dev mode).
    """

    def __init__(self, app, expected_token: str | None):
        super().__init__(app)
        self.expected_token = expected_token

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if not self.expected_token:
            return await call_next(request)

        header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization header"},
                status_code=401,
            )
        token = header[7:].strip()
        if token != self.expected_token:
            return JSONResponse({"error": "invalid token"}, status_code=401)
        return await call_next(request)


async def healthz(request: Request) -> Response:
    """Health probe endpoint. No auth required."""
    return JSONResponse({
        "status": "ok",
        "ibkr_connected": ibkr_client.is_connected(),
        "reversal_strategies": len(ibkr_client._reversal_states or {}) if ibkr_client._reversal_states is not None else 0,
        "swing_strategies": len(ibkr_client._swing_states or {}) if ibkr_client._swing_states is not None else 0,
    })


def validate_binding(host: str, token: str | None) -> None:
    """Refuse to start unsafely.

    Non-localhost bind without a token is rejected — the token is the
    last-line defense if firewall rules change underneath us.
    """
    safe_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in safe_hosts and not token:
        raise RuntimeError(
            f"REFUSING TO START: MCP_BIND_HOST={host!r} is non-localhost "
            "but MCP_AUTH_TOKEN is empty. Set a strong random token in the "
            "environment before exposing the MCP endpoint to a network."
        )


def build_starlette_app(
    session_manager: StreamableHTTPSessionManager,
    auth_token: str | None,
) -> Starlette:
    """Construct the Starlette ASGI app."""
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Mount("/mcp", app=session_manager.handle_request),
    ]
    middleware = [Middleware(BearerAuthMiddleware, expected_token=auth_token)]
    return Starlette(routes=routes, middleware=middleware)


async def run_http_server(host: str, port: int, auth_token: str | None) -> None:
    """Run the MCP HTTP server until cancelled."""
    validate_binding(host, auth_token)

    session_manager = StreamableHTTPSessionManager(server)
    app = build_starlette_app(session_manager, auth_token)

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    srv = uvicorn.Server(config)

    async with session_manager.run():
        logger.info(f"MCP HTTP transport listening on {host}:{port}")
        logger.info(
            "auth: " + (
                "enabled (bearer token required)"
                if auth_token else "DISABLED (localhost-only mode)"
            )
        )
        await srv.serve()
