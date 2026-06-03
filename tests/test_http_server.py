"""Tests for Layer 5b — HTTP transport.

Covers:
  - Bearer auth middleware blocks/allows correctly
  - `/healthz` is always reachable regardless of auth
  - `validate_binding` refuses non-localhost without a token
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ibkr_mcp_server.http_server import (
    BearerAuthMiddleware,
    healthz,
    validate_binding,
)


# --- helpers --------------------------------------------------------------


def _build_app(token: str | None) -> Starlette:
    """Tiny Starlette app with healthz + a protected /tools endpoint
    + chat HTML/static/API stand-ins so we can test public-path logic.

    The middleware allows /chat, /chat/static/*, and /healthz unconditionally.
    Everything else (including /tools and /chat/api/*) requires auth.
    """
    async def tools_endpoint(request):
        return JSONResponse({"tools": ["one", "two"]})

    async def chat_html_endpoint(request):
        return JSONResponse({"page": "html"})

    async def chat_static_endpoint(request):
        return JSONResponse({"asset": request.path_params.get("file")})

    async def chat_api_endpoint(request):
        return JSONResponse({"api": "ok"})

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/tools", tools_endpoint, methods=["GET"]),
        Route("/chat", chat_html_endpoint, methods=["GET"]),
        Route("/chat/static/{file:path}", chat_static_endpoint, methods=["GET"]),
        Route("/chat/api/threads", chat_api_endpoint, methods=["GET"]),
    ]
    return Starlette(
        routes=routes,
        middleware=[Middleware(BearerAuthMiddleware, expected_token=token)],
    )


# --- bearer auth middleware ------------------------------------------------


class TestBearerAuth:
    def test_healthz_always_open(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "ibkr_connected" in body
        assert "swing_strategies" in body
        assert "reversal_strategies" in body

    def test_protected_route_rejects_missing_header(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools")
        assert r.status_code == 401
        assert "Authorization" in r.json()["error"]

    def test_protected_route_rejects_malformed_header(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools", headers={"Authorization": "Token notbearer"})
        assert r.status_code == 401

    def test_protected_route_rejects_wrong_token(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        assert "invalid token" in r.json()["error"]

    def test_protected_route_accepts_correct_token(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200

    def test_no_token_configured_allows_all(self):
        client = TestClient(_build_app(token=None))
        r = client.get("/tools")
        assert r.status_code == 200

    def test_case_insensitive_header_name(self):
        # Starlette TestClient normalizes header names, but our middleware
        # explicitly checks both lower- and Title-case for resilience to
        # ASGI-server quirks. This confirms both work.
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools", headers={"authorization": "Bearer secret"})
        assert r.status_code == 200

    # --- query-param fallback (for browser-navigation case) ---------------
    #
    # The /chat UI is loaded by typing a URL into the browser. Browsers do
    # NOT attach Authorization headers to navigation GETs (only to
    # fetch()). Without a fallback, the very first /chat request 401s
    # and the page never loads. The middleware accepts ?token=... as a
    # backup so the page can boot, save the token to localStorage, and
    # use the Authorization header for every subsequent API call.

    def test_query_param_token_accepted_when_header_absent(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools?token=secret")
        assert r.status_code == 200

    def test_query_param_token_rejected_when_wrong(self):
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools?token=nope")
        assert r.status_code == 401
        assert "invalid token" in r.json()["error"]

    def test_header_takes_precedence_over_query_param(self):
        """If both are present and the header is valid, the request goes
        through even if the query is bogus -- header is preferred."""
        client = TestClient(_build_app(token="secret"))
        r = client.get(
            "/tools?token=garbage",
            headers={"Authorization": "Bearer secret"},
        )
        assert r.status_code == 200

    def test_query_param_does_not_bypass_token_check(self):
        """Sanity: query param must still match. Empty query + no header
        is still 401."""
        client = TestClient(_build_app(token="secret"))
        r = client.get("/tools?token=")
        assert r.status_code == 401

    # --- public-path bypass ----------------------------------------------
    #
    # The chat HTML shell + its static assets must be reachable without
    # any auth at all -- it's an SPA bootstrap that asks the user for
    # the token via a JS prompt, then uses that token for the
    # /chat/api/* fetches it makes. Browser users often can't get a
    # 64-char hex token correctly into the URL bar; the prompt path is
    # the reliable fallback.

    def test_chat_html_page_is_public(self):
        """GET /chat must succeed without auth (no Bearer, no ?token=)."""
        client = TestClient(_build_app(token="secret"))
        r = client.get("/chat")
        assert r.status_code == 200
        assert r.json() == {"page": "html"}

    def test_chat_static_assets_are_public(self):
        """Static assets (CSS, JS, manifest, icon) must be public so the
        chat page can load them before the user enters a token."""
        client = TestClient(_build_app(token="secret"))
        for asset in ("manifest.json", "icon.svg", "chat.js"):
            r = client.get(f"/chat/static/{asset}")
            assert r.status_code == 200, f"asset {asset} should be public"

    def test_chat_api_endpoints_still_require_auth(self):
        """Page is public; APIs are not. The whole point of the split."""
        client = TestClient(_build_app(token="secret"))
        # No header, no query token -> 401
        r = client.get("/chat/api/threads")
        assert r.status_code == 401
        # Valid bearer -> 200
        r = client.get(
            "/chat/api/threads", headers={"Authorization": "Bearer secret"}
        )
        assert r.status_code == 200


# --- startup-binding validation --------------------------------------------


class TestValidateBinding:
    def test_localhost_no_token_allowed(self):
        # Three forms of localhost should all be safe without a token
        for host in ("127.0.0.1", "localhost", "::1"):
            validate_binding(host, None)            # should not raise

    def test_localhost_with_token_allowed(self):
        validate_binding("127.0.0.1", "anything")

    def test_public_bind_without_token_rejected(self):
        with pytest.raises(RuntimeError, match="REFUSING TO START"):
            validate_binding("0.0.0.0", None)
        with pytest.raises(RuntimeError, match="REFUSING TO START"):
            validate_binding("0.0.0.0", "")
        with pytest.raises(RuntimeError, match="REFUSING TO START"):
            validate_binding("192.168.1.10", None)

    def test_public_bind_with_token_allowed(self):
        validate_binding("0.0.0.0", "secret")
        validate_binding("10.0.0.5", "x" * 32)
