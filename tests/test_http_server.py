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
    """Tiny Starlette app with healthz + a protected /tools endpoint.

    We can't test the real /mcp mount easily without a full MCP session, so
    we substitute a stand-in route that the middleware will treat the same way.
    """
    async def tools_endpoint(request):
        return JSONResponse({"tools": ["one", "two"]})

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/tools", tools_endpoint, methods=["GET"]),
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
