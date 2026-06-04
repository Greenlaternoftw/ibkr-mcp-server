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
import sqlite3
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

import asyncio

from ..config import settings
from ..tools import TOOLS, call_tool as mcp_call_tool
from . import auth_pin
from .agent import AnthropicAgent, ChatError, StreamEvent
from .persistence import ChatStore
from .prompts import SYSTEM_PROMPT
from .pubsub import ThreadEventBus
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


# Singleton ThreadEventBus per daemon process. Subscribers (open SSE
# connections) receive every event published from any mutating endpoint.
# In-memory only -- no cross-process fanout. See chat/pubsub.py for the
# design rationale.
_bus: Optional[ThreadEventBus] = None


def _get_bus() -> ThreadEventBus:
    global _bus
    if _bus is None:
        _bus = ThreadEventBus()
    return _bus


def reset_bus() -> None:
    """Test hook -- forces a fresh bus on next request."""
    global _bus
    _bus = None


async def _publish(event_type: str, *, client_id: Optional[str] = None, **fields) -> None:
    """Publish a thread event to the bus.

    All sync events go through this helper so the shape stays consistent
    and originating_client_id is always populated (or None) -- the client
    relies on that field to skip its own echoes.
    """
    try:
        await _get_bus().publish(
            {
                "type": event_type,
                "originating_client_id": client_id,
                **fields,
            }
        )
    except Exception:
        # Publishing must never break the user-facing request. The
        # browser will resync on next user action even if a push is
        # missed.
        logger.exception("failed to publish %s event", event_type)


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


def _effective_pin() -> Optional[str]:
    """Return the currently active PIN.

    Resolution order:
      1. SQLite ``auth_config.pin`` -- set via the change-PIN UI; once
         present, it always wins (operator's most recent intent).
      2. ``settings.chat_pin`` -- the CHAT_PIN env var, used as the
         initial seed before anyone has changed it via the UI.

    Returns None when neither has a PIN configured -- in which case
    pin_status reports configured=false and the UI falls back to the
    raw-token prompt.
    """
    db_pin = _get_store().get_pin()
    if db_pin:
        return db_pin
    return settings.chat_pin or None


async def pin_status(request: Request) -> Response:
    """PUBLIC. Tells the UI whether a PIN is configured.

    Returns ``{"configured": true/false}``. No auth required -- the
    response leaks only whether the operator has set up PIN unlock,
    which is something a determined network observer could infer anyway
    (e.g., by attempting unlock and seeing 404 vs 401).
    """
    return JSONResponse({"configured": bool(_effective_pin())})


async def pin_unlock(request: Request) -> Response:
    """PUBLIC, rate-limited. Trades a correct PIN for the bearer token.

    Body: ``{"pin": "1234"}``. On success returns
    ``{"token": "<MCP_AUTH_TOKEN>"}`` and the page saves it to
    localStorage. On failure, returns 401 with an error string and
    records a failure in the in-memory rate limiter.

    Status semantics:
      * **200** ``{token}``                   correct PIN
      * **400** ``{error}``                   bad request body
      * **401** ``{error}``                   wrong PIN
      * **404** ``{error: "not configured"}`` CHAT_PIN env var is unset
      * **429** ``{error}``                   rate-limited or locked out
    """
    effective_pin = _effective_pin()
    if not effective_pin:
        return JSONResponse(
            {"error": "PIN not configured; set CHAT_PIN in .env or use the change-PIN UI"},
            status_code=404,
        )

    # Pre-check the throttle before even reading the body. A flood of
    # malformed requests should not get a free pass past the limiter.
    pre_status = auth_pin.status()
    if pre_status == "locked_out":
        return JSONResponse(
            {
                "error": (
                    "PIN unlock locked for the rest of the hour. "
                    "Use your bearer token to log in if you need access now."
                )
            },
            status_code=429,
        )
    if pre_status == "rate_limited":
        return JSONResponse(
            {"error": "too many recent attempts -- wait a minute"},
            status_code=429,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    pin = body.get("pin")
    if not isinstance(pin, str) or not pin:
        return JSONResponse({"error": "body must include 'pin' string"}, status_code=400)

    # Constant-time compare -- the PIN is short enough that timing
    # attacks are theoretically possible. ``secrets.compare_digest``
    # avoids leaking how many leading chars matched.
    import secrets as _secrets
    if not _secrets.compare_digest(pin, effective_pin):
        new_status = auth_pin.record_failure()
        if new_status == "locked_out":
            auth_pin.maybe_alert_lockout()
        return JSONResponse({"error": "incorrect PIN"}, status_code=401)

    # Success -- clear the failure history so the next user doesn't
    # inherit a near-miss attacker's counters.
    auth_pin.record_success()
    return JSONResponse({"token": settings.mcp_auth_token or ""})


async def pin_change(request: Request) -> Response:
    """REQUIRES BEARER AUTH. Rotate the PIN.

    Body: ``{"old_pin": "1234", "new_pin": "5678"}``.

    Goes through the middleware's normal bearer auth (Authorization
    header, NOT the public PIN-status/unlock path), so an attacker
    without the bearer token can't change the PIN even if they guess
    the old one. The old PIN check is belt-and-suspenders for cases
    where the bearer token leaks but the operator's phone is intact --
    they still need a moment of physical access.

    On success the new PIN immediately takes effect for all future
    unlock attempts (no daemon restart needed -- next call to
    _effective_pin() reads the new value from SQLite).

    Status semantics:
      * **200** ``{ok: true}``                       PIN changed
      * **400** ``{error}``                          bad body / new_pin too short
      * **401** ``{error}``                          old_pin wrong
      * **404** ``{error}``                          no PIN currently set (use unlock-first flow)
      * **429** ``{error}``                          rate-limited / locked out
    """
    effective = _effective_pin()
    if not effective:
        return JSONResponse(
            {"error": "no PIN configured to change; set CHAT_PIN in .env first"},
            status_code=404,
        )

    # Reuse the unlock throttle so a "guess old PIN" attack costs the
    # same as a "guess current PIN" attack.
    pre_status = auth_pin.status()
    if pre_status == "locked_out":
        return JSONResponse(
            {"error": "PIN endpoints locked for the rest of the hour"},
            status_code=429,
        )
    if pre_status == "rate_limited":
        return JSONResponse(
            {"error": "too many recent PIN attempts -- wait a minute"},
            status_code=429,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    old_pin = body.get("old_pin")
    new_pin = body.get("new_pin")
    if not isinstance(old_pin, str) or not isinstance(new_pin, str):
        return JSONResponse(
            {"error": "body must include 'old_pin' and 'new_pin' strings"},
            status_code=400,
        )
    new_pin = new_pin.strip()
    if len(new_pin) < 4:
        return JSONResponse(
            {"error": "new_pin must be at least 4 characters"},
            status_code=400,
        )

    import secrets as _secrets
    if not _secrets.compare_digest(old_pin, effective):
        new_status = auth_pin.record_failure()
        if new_status == "locked_out":
            auth_pin.maybe_alert_lockout()
        return JSONResponse({"error": "old PIN is incorrect"}, status_code=401)

    # Old PIN verified, new PIN meets length requirement. Persist.
    _get_store().set_pin(new_pin)
    # Reset throttle counters on success -- treat this as a "verified
    # operator action" the same way pin_unlock does.
    auth_pin.record_success()
    return JSONResponse({"ok": True, "message": "PIN updated"})


async def prefs_list(request: Request) -> Response:
    """Bulk read all user prefs. UI calls this once on page boot to
    avoid a round-trip per individual key."""
    return JSONResponse({"prefs": _get_store().list_prefs()})


async def prefs_set(request: Request) -> Response:
    """Upsert a single pref. Body: ``{key, value, client_id?}``. Value
    is opaque to the server -- the UI JSON-encodes anything structured.

    Emits a `pref_changed` SSE event so other tabs / devices invalidate
    their local pref cache and pick up the new value (critical for
    cross-device chat thread sync via the activeThreadId pref).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    key = body.get("key")
    value = body.get("value")
    client_id = body.get("client_id")
    if not isinstance(key, str) or not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    if not isinstance(value, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    try:
        _get_store().set_pref(key, value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    await _publish("pref_changed", client_id=client_id, key=key, value=value)
    return JSONResponse({"ok": True, "key": key})


async def prefs_delete(request: Request) -> Response:
    key = request.path_params["key"]
    _get_store().delete_pref(key)
    # client_id query param is optional -- when present we suppress the
    # originating tab's echo of its own delete.
    client_id = request.query_params.get("client_id")
    await _publish("pref_changed", client_id=client_id, key=key, value=None)
    return Response(status_code=204)


async def pivot_loops_list(request: Request) -> Response:
    """List active pivot loops (optionally include stopped via ?include_stopped=1)."""
    include = request.query_params.get("include_stopped", "0") in ("1", "true", "yes")
    loops = _get_store().list_pivot_loops(include_stopped=include)
    return JSONResponse({"loops": loops})


async def pivot_loops_create(request: Request) -> Response:
    """Start a new pivot loop.  Body:
       ``{symbol, initial_capital, lookback_days, compound?,
          entry_price?, target_price?, stop_price?,
          catalyst_horizon_days?, max_drawdown_pct?, notes?}``
    Fails 409 if a loop already exists for that symbol.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    symbol = (body.get("symbol") or "").strip().upper()
    try:
        initial_capital = float(body.get("initial_capital"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "initial_capital required"}, status_code=400)
    try:
        lookback_days = int(body.get("lookback_days"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "lookback_days required"}, status_code=400)

    try:
        loop = _get_store().create_pivot_loop(
            symbol,
            initial_capital=initial_capital,
            lookback_days=lookback_days,
            compound=bool(body.get("compound", True)),
            entry_price=body.get("entry_price"),
            target_price=body.get("target_price"),
            stop_price=body.get("stop_price"),
            catalyst_horizon_days=int(body.get("catalyst_horizon_days", 2)),
            max_drawdown_pct=float(body.get("max_drawdown_pct", 50.0)),
            notes=body.get("notes"),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": f"a loop already exists for {symbol}; stop it first"},
            status_code=409,
        )
    # Spawn the autonomous tick task. Idempotent -- if a task already
    # exists (e.g. operator deleted+recreated quickly), the engine
    # returns "already_running" without spawning a duplicate.
    try:
        from ..client import ibkr_client
        await ibkr_client.start_pivot_loop_task(symbol)
    except Exception as e:
        logger.warning(f"pivot loop {symbol} created but engine spawn failed: {e}")
    await _publish(
        "pivot_loop_changed",
        client_id=body.get("client_id"),
        symbol=symbol,
        action="created",
    )
    return JSONResponse(loop, status_code=201)


async def pivot_loop_get(request: Request) -> Response:
    """Return a loop's state + cycle history (most recent first)."""
    sym = (request.path_params.get("symbol") or "").strip().upper()
    store = _get_store()
    loop = store.get_pivot_loop(sym)
    if loop is None:
        return JSONResponse({"error": "no loop for that symbol"}, status_code=404)
    cycles = store.get_pivot_loop_cycles(sym, limit=50)
    return JSONResponse({"loop": loop, "cycles": cycles})


async def pivot_loop_patch(request: Request) -> Response:
    """Update mutable fields of a loop. Used by the chat agent's
    update_pivot_loop_state MCP tool when tracking entry/exit progress.
    Body is a flat object of allowed fields; unknown keys → 400.
    """
    sym = (request.path_params.get("symbol") or "").strip().upper()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    client_id = body.pop("client_id", None)
    try:
        out = _get_store().update_pivot_loop(sym, **body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if out is None:
        return JSONResponse({"error": "no loop for that symbol"}, status_code=404)
    await _publish(
        "pivot_loop_changed", client_id=client_id, symbol=sym, action="updated",
    )
    return JSONResponse(out)


async def pivot_loop_record_cycle(request: Request) -> Response:
    """Append a completed cycle to the loop. Body must include the cycle's
    entry/exit data and realized_pnl. Atomically updates roll-up counters
    + (if compounding) the current_capital.
    """
    sym = (request.path_params.get("symbol") or "").strip().upper()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        loop = _get_store().record_pivot_loop_cycle(
            sym,
            capital_at_start=float(body["capital_at_start"]),
            entry_price=body.get("entry_price"),
            entry_fill=body.get("entry_fill"),
            entry_at=body.get("entry_at"),
            shares=body.get("shares"),
            exit_fill=body.get("exit_fill"),
            exit_at=body.get("exit_at"),
            exit_reason=body.get("exit_reason"),
            realized_pnl=float(body["realized_pnl"]),
        )
    except (KeyError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    await _publish(
        "pivot_loop_changed",
        client_id=body.get("client_id"),
        symbol=sym,
        action="cycle_recorded",
    )
    return JSONResponse(loop)


async def pivot_loop_stop(request: Request) -> Response:
    """Mark a loop as stopped. Row + cycle history preserved for review.
    Also cancels the engine tick task. Returns the final state."""
    sym = (request.path_params.get("symbol") or "").strip().upper()
    out = _get_store().stop_pivot_loop(sym)
    if out is None:
        return JSONResponse(
            {"error": "no active loop for that symbol"}, status_code=404
        )
    # Cancel the autonomous tick task -- engine.stop_pivot_loop_task is
    # idempotent so a double-stop just yields "not_running".
    try:
        from ..client import ibkr_client
        await ibkr_client.stop_pivot_loop_task(sym)
    except Exception as e:
        logger.warning(f"pivot loop {sym} marked stopped but task cancel failed: {e}")
    await _publish(
        "pivot_loop_changed",
        client_id=request.query_params.get("client_id"),
        symbol=sym,
        action="stopped",
    )
    return JSONResponse(out)


async def pivot_analysis(request: Request) -> Response:
    """Pivot-loop analysis for the Command Center "Loop" tab.

    GET /chat/api/pivot/{symbol}?lookback=N

    lookback (default 7) is the number of daily bars to use for pivot
    low / pivot high / average rise computation. The catalyst feed is
    queried for events in the next max(lookback, 30) days so the
    operator can see an event coming even with a short lookback.
    """
    sym = (request.path_params.get("symbol") or "").strip().upper()
    if not sym or len(sym) > 8:
        return JSONResponse({"error": "invalid symbol"}, status_code=400)

    try:
        lookback = int(request.query_params.get("lookback", "7"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "lookback must be an integer"}, status_code=400)
    if not (3 <= lookback <= 180):
        return JSONResponse(
            {"error": "lookback must be 3-180 days"}, status_code=400
        )

    from ..client import ibkr_client
    from .. import pivot as pivot_mod
    from .. import catalysts as cat_mod

    # Pull a few extra days of buffer (weekends/holidays trim the result).
    try:
        bars = await ibkr_client.get_historical_bars(
            sym, lookback_days=lookback + 5
        )
    except Exception as e:
        logger.exception(f"pivot: historical bars failed for {sym}")
        return JSONResponse(
            {"error": f"historical bars failed: {e}"}, status_code=502
        )

    # Trim to the requested lookback window.
    if len(bars) > lookback:
        bars = bars.tail(lookback).reset_index(drop=True)

    # Best-effort catalyst fetch -- yfinance can fail silently; the
    # pivot analysis still works without it (no catalyst block).
    catalysts = cat_mod.get_upcoming_catalysts(
        sym, horizon_days=max(lookback, 30)
    )

    # Phase E -- broader market regime (SPY/VIX gate). Cached 1h. None
    # if the fetch fails -- in that case the gate is skipped, matching
    # the engine's behavior.
    from .. import pivot_loop as engine_mod
    market_regime_enabled = await engine_mod.get_market_regime_enabled(ibkr_client)

    try:
        analysis = pivot_mod.analyze_pivot_loop(
            bars, catalysts,
            market_regime_enabled=market_regime_enabled,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    out = pivot_mod.to_json_dict(analysis)
    out["symbol"] = sym
    out["lookback_days"] = lookback
    return JSONResponse(out)


async def positions(request: Request) -> Response:
    """Current IBKR positions for the active account, shaped for the
    Command Center "Portfolio" tab auto-sync. The client polls this
    every 30s and reconciles each portfolio-type watchlist against
    the returned list.

    Uses ``ib.portfolio()`` (PortfolioItem stream from account-update
    subscription) NOT ``reqPositions`` (bare Position namedtuple with
    no marketPrice). The first gives us live mark + unrealized P&L
    for free; the second left every metric at 0.0.

    Response: ``{"positions": [{"symbol", "quantity", "avg_cost",
    "market_price", "market_value", "unrealized_pnl", "realized_pnl"},
    ...]}``. Equity-only filter applied -- we skip OPT/FUT/etc. for
    the dashboard tab (they have dedicated tools).
    """
    from ..client import ibkr_client
    try:
        # Make sure the IBKR connection + account-update subscription
        # is live. get_account_summary() side-effect-subscribes; calling
        # it (and discarding the result) is the cheapest way to ensure
        # portfolio() is populated.
        if not await ibkr_client._ensure_connected():
            return JSONResponse({"error": "Not connected to IBKR"}, status_code=503)
        await ibkr_client.get_account_summary()
        target = ibkr_client.current_account
        items = ibkr_client.ib.portfolio()
    except Exception as e:
        logger.exception("positions fetch failed")
        return JSONResponse({"error": str(e)}, status_code=500)

    out = []
    for it in items or []:
        # Per-account filter: PortfolioItem has an `account` field.
        if target and getattr(it, "account", None) != target:
            continue
        contract = getattr(it, "contract", None)
        if contract is None:
            continue
        if (getattr(contract, "secType", "") or "").upper() != "STK":
            continue
        qty = float(getattr(it, "position", 0) or 0)
        if not qty:
            continue
        out.append({
            "symbol": contract.symbol,
            "quantity": qty,
            "avg_cost": float(getattr(it, "averageCost", 0) or 0),
            "market_price": float(getattr(it, "marketPrice", 0) or 0),
            "market_value": float(getattr(it, "marketValue", 0) or 0),
            "unrealized_pnl": float(getattr(it, "unrealizedPNL", 0) or 0),
            "realized_pnl": float(getattr(it, "realizedPNL", 0) or 0),
        })
    return JSONResponse({"positions": out})


async def account_summary(request: Request) -> Response:
    """Compact account-summary JSON for the Command Center strip.

    Wraps IBKRClient.get_account_summary into the small set of fields
    the UI displays: NetLiq, Buying Power, Excess Liquidity, Maint
    Margin, Realized P/L, Unrealized P/L. The strip polls this every
    10-15 seconds; we don't bother with caching beyond that.
    """
    from ..client import ibkr_client
    try:
        raw = await ibkr_client.get_account_summary()
    except Exception as e:
        logger.exception("account summary fetch failed")
        return JSONResponse({"error": str(e)}, status_code=500)

    # get_account_summary() returns {"account": ..., "as_of": ..., "summary": {tag: value}}
    # -- the IBKR tag/value pairs are nested under `summary`, NOT at the top level.
    summary = raw.get("summary") or {}

    def _num(key):
        v = summary.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return JSONResponse({
        "account": raw.get("account"),
        "net_liquidation": _num("NetLiquidation"),
        "buying_power": _num("BuyingPower"),
        "excess_liquidity": _num("ExcessLiquidity"),
        "maint_margin": _num("MaintMarginReq"),
        "realized_pnl": _num("RealizedPnL"),
        "unrealized_pnl": _num("UnrealizedPnL"),
        "total_cash": _num("TotalCashValue"),
    })


# --- watchlists / portfolios --------------------------------------------


async def watchlists_list(request: Request) -> Response:
    return JSONResponse({"watchlists": _get_store().list_watchlists()})


async def watchlists_create(request: Request) -> Response:
    try:
        body = await request.json() if await request.body() else {}
    except json.JSONDecodeError:
        body = {}
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    if len(name) > 64:
        return JSONResponse({"error": "name too long (max 64)"}, status_code=400)
    import sqlite3
    try:
        wl = _get_store().create_watchlist(name)
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "watchlist with that name already exists"}, status_code=409)
    return JSONResponse(wl, status_code=201)


async def watchlists_delete(request: Request) -> Response:
    wid = int(request.path_params["wid"])
    if not _get_store().delete_watchlist(wid):
        return JSONResponse({"error": "watchlist not found"}, status_code=404)
    return Response(status_code=204)


async def watchlist_stocks_list(request: Request) -> Response:
    wid = int(request.path_params["wid"])
    return JSONResponse({"stocks": _get_store().get_watchlist_stocks(wid)})


async def watchlist_stocks_add(request: Request) -> Response:
    wid = int(request.path_params["wid"])
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    sym = (body.get("symbol") or "").strip().upper()
    if not sym or len(sym) > 8:
        return JSONResponse({"error": "symbol required (max 8 chars)"}, status_code=400)
    import sqlite3
    try:
        row = _get_store().add_watchlist_stock(wid, sym)
    except sqlite3.IntegrityError:
        return JSONResponse({"error": f"{sym} already in this watchlist"}, status_code=409)
    return JSONResponse(row, status_code=201)


async def watchlist_stocks_update(request: Request) -> Response:
    """Upsert metrics for a stock already in the watchlist. Used by the
    UI to write back research/price-refresh data so it survives reload."""
    wid = int(request.path_params["wid"])
    sym = request.path_params["symbol"].upper()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    row = _get_store().upsert_watchlist_stock(
        wid, sym,
        rating=body.get("rating"),
        current_price=body.get("current_price"),
        target_price=body.get("target_price"),
        range_low=body.get("range_low"),
        range_high=body.get("range_high"),
        notes=body.get("notes"),
    )
    if row is None:
        return JSONResponse({"error": "stock not in watchlist"}, status_code=404)
    return JSONResponse(row)


async def watchlist_stocks_remove(request: Request) -> Response:
    wid = int(request.path_params["wid"])
    sym = request.path_params["symbol"].upper()
    if not _get_store().remove_watchlist_stock(wid, sym):
        return JSONResponse({"error": "stock not in watchlist"}, status_code=404)
    return Response(status_code=204)


# --- live market quote -------------------------------------------------


async def research_symbol(request: Request) -> Response:
    """Structured analyst-and-think-tank research on one symbol.

    The Command Center UI used to call Anthropic API directly for this
    with a giant inline prompt + web_search tool. We do it server-side
    now so:
      * the API key never leaves the VPS,
      * the prompt cache works across requests (same prompt prefix
        every time),
      * we can serve the JSON-only response shape the UI expects
        without exposing it to model refusals or wrapper preamble.

    Returns the parsed JSON object verbatim if the model produced one,
    or a structured error.
    """
    sym = (request.path_params.get("symbol") or "").strip().upper()
    if not sym or len(sym) > 8:
        return JSONResponse({"error": "invalid symbol"}, status_code=400)

    if not settings.anthropic_api_key:
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not set; configure .env and restart"},
            status_code=503,
        )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = f"""For the ticker {sym}, return ONLY a JSON object (no markdown, no preamble) with this exact shape:

{{
  "symbol": "{sym}",
  "currentPrice": <number>,
  "consensusTarget": <number, 12-month consensus price target>,
  "analysts": [
    {{ "name": "<analyst full name>", "firm": "<firm>", "rating": "BUY|HOLD|SELL|OUTPERFORM|UNDERPERFORM", "priceTarget": <number>, "accuracy": <integer 0-100, the analyst's published success rate>, "dateUpdated": "YYYY-MM-DD" }}
  ],
  "thinkTanks": [
    {{ "name": "<institution>", "view": "<one-sentence positioning>", "stance": "POSITIVE|NEGATIVE|NEUTRAL", "accuracy": <integer 0-100 estimate>, "asOf": "YYYY-MM-DD" }}
  ],
  "impliedMoves": {{
    "available": <true if options-implied move data was found, false otherwise>,
    "30d": <decimal, e.g. 0.045 for 4.5%; one-standard-deviation expected move from ATM options>,
    "60d": <decimal>,
    "90d": <decimal>,
    "source": "<short note on where the data came from>"
  }},
  "news": [
    {{ "headline": "<short headline>", "impact": "POSITIVE|NEGATIVE|NEUTRAL", "magnitude": <1-5>, "rationale": "<one sentence>" }}
  ]
}}

Requirements:
- Use web search to gather data. Use accurate, current information.
- Include the top 6-8 analysts ranked by accuracy (success rate) descending.
- Include 2-4 think-tank / institutional views.
- For impliedMoves: search Barchart, OptionStrat, Market Chameleon, IBKR, or similar for ATM straddle / IV-derived expected moves at the ~30/60/90 day horizons. If not found, set "available": false and use 0 for values.
- Include 3-5 recent news items that could move the price in the next 90 days.
- If you cannot find a specific analyst's accuracy, estimate based on firm tier (Tier-1 firms 70-78%, top boutiques 75-85%) and mark in the name as "(est.)".
- Numbers only, no strings with $ signs.
- Return ONLY the JSON object."""

    try:
        msg = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4000,
            # Cache the (large, static) prompt prefix -- same shape on
            # every research request, so subsequent calls hit cache.
            system=[{
                "type": "text",
                "text": "You are a financial-data research assistant. Return ONLY valid JSON matching the shape requested. No markdown, no preamble.",
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
        )
    except Exception as e:
        logger.exception(f"research call to Anthropic failed for {sym}")
        return JSONResponse({"error": f"upstream Anthropic call failed: {e}"}, status_code=502)

    # Extract text from response blocks (ignoring tool_use blocks).
    text_parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", "") or "")
    text = "\n".join(text_parts).strip()
    if not text:
        return JSONResponse(
            {"error": "Anthropic returned no text blocks (probably mid-tool-loop)",
             "raw_blocks": [getattr(b, "type", "?") for b in msg.content]},
            status_code=502,
        )

    # Strip markdown fences if any, then find the JSON object.
    text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return JSONResponse(
            {"error": "no JSON object in model response",
             "preview": text[:300]},
            status_code=502,
        )

    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return JSONResponse(
            {"error": f"JSON parse failed: {e}", "preview": text[:300]},
            status_code=502,
        )

    return JSONResponse({
        "status": "ok",
        "symbol": sym,
        "data": parsed,
        "usage": {
            "input_tokens": getattr(msg.usage, "input_tokens", 0),
            "output_tokens": getattr(msg.usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        },
    })


async def market_quote(request: Request) -> Response:
    """Single-symbol quote (last/bid/ask) for the price ticker.

    Calls IBKRClient.get_market_data if available, falls back to
    placeholder if not connected or rate-limited. UI is expected to
    handle missing-data gracefully (shows '—').
    """
    sym = (request.query_params.get("symbol") or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "symbol query param required"}, status_code=400)

    from ..client import ibkr_client
    if not ibkr_client.is_connected():
        return JSONResponse(
            {"symbol": sym, "error": "IBKR not connected", "last": None,
             "bid": None, "ask": None},
            status_code=503,
        )

    # IBKRClient has get_shortable_shares + get_margin_requirements that
    # internally fetch ticker data. We don't have a clean "just give me
    # the last price" method yet, so we use a minimal helper: subscribe
    # briefly via reqMktData. Keep this cheap.
    try:
        from ib_async import Stock as _Stock
        contract = _Stock(sym, "SMART", "USD")
        await ibkr_client._bounded(
            ibkr_client.ib.qualifyContractsAsync(contract),
            timeout=ibkr_client.QUALIFY_TIMEOUT,
            op=f"qualify_quote:{sym}",
        )
        if not contract.conId:
            return JSONResponse(
                {"symbol": sym, "error": "could not qualify contract"},
                status_code=404,
            )
        ticker = ibkr_client.ib.reqMktData(contract, "", False, False)
        # Brief poll for a snapshot. Cancel cleanly afterwards so we
        # don't leak subscriptions.
        for _ in range(20):  # ~2 seconds max
            await asyncio.sleep(0.1)
            if ticker.last or ticker.close or (ticker.bid and ticker.ask):
                break
        last = ticker.last if ticker.last and ticker.last > 0 else None
        close = ticker.close if ticker.close and ticker.close > 0 else None
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else None
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        ibkr_client.ib.cancelMktData(contract)
        return JSONResponse({
            "symbol": sym,
            "last": last or close,
            "bid": bid,
            "ask": ask,
            "close": close,
        })
    except Exception as e:
        logger.exception(f"market quote failed for {sym}")
        return JSONResponse({"symbol": sym, "error": str(e)}, status_code=500)


async def events_stream(request: Request) -> Response:
    """Long-lived SSE stream of thread mutation events.

    Browsers open one connection per tab on page load and keep it open
    for the life of the tab. Events are pushed as
    ``data: <json>\\n\\n`` lines; each JSON has at least ``type`` and
    ``originating_client_id`` (the latter so the originating tab can
    ignore its own echoes).

    Heartbeat every 25s (well under typical proxy 30s read timeouts
    and well under iOS's 30s background-tab kill).
    """
    bus = _get_bus()

    async def event_source():
        async with bus.subscribe() as queue:
            # Initial line so the client knows the connection is live.
            # Comment lines (starting with `:`) are ignored by EventSource
            # but flushed through the proxy chain, so they confirm the
            # full path is unbuffered.
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    # Heartbeat. Doesn't fire an EventSource.onmessage.
                    yield ": ping\n\n"
                    continue
                except asyncio.CancelledError:
                    return
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def list_threads(request: Request) -> Response:
    """List all conversation threads, most-recent first."""
    threads = _get_store().list_threads()
    return JSONResponse({"threads": threads})


async def create_thread(request: Request) -> Response:
    """Create a new thread. Body: ``{"title": "...", "client_id": "..."}``."""
    try:
        body = await request.json() if await request.body() else {}
    except json.JSONDecodeError:
        body = {}
    title = (body.get("title") or "").strip() or "New chat"
    client_id = body.get("client_id")
    thread = _get_store().create_thread(title)
    await _publish(
        "thread_created",
        client_id=client_id,
        thread_id=thread["id"],
    )
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
    """Rename a thread. Body: ``{"title": "...", "client_id": "..."}``."""
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
    await _publish(
        "thread_renamed",
        client_id=body.get("client_id"),
        thread_id=thread_id,
        title=new_title,
    )
    return JSONResponse({"ok": True, "id": thread_id, "title": new_title})


async def delete_thread(request: Request) -> Response:
    """Hard-delete a thread and all its messages.

    ``client_id`` accepted as a query param (since DELETE bodies aren't
    universally supported by every browser fetch implementation).
    """
    thread_id = request.path_params["thread_id"]
    client_id = request.query_params.get("client_id")
    if not _get_store().delete_thread(thread_id):
        return JSONResponse({"error": "thread not found"}, status_code=404)
    await _publish(
        "thread_deleted",
        client_id=client_id,
        thread_id=thread_id,
    )
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

    client_id = body.get("client_id")

    async def event_source():
        # Outer try is the last line of defence -- if the agent crashes
        # before yielding anything, the client still gets an error event
        # instead of an opaque connection close.
        try:
            async for event in agent.run_stream(messages):
                # Snapshot the conversation on `done` so we can persist
                # the canonical post-turn state (includes any tool_use
                # / tool_result blocks the agent appended), then publish
                # so other tabs/devices see the change.
                if event.type == "done" and thread_id:
                    try:
                        _get_store().replace_messages(
                            thread_id, event.payload.get("conversation") or []
                        )
                    except Exception:
                        logger.exception(
                            "failed to persist chat thread %s", thread_id
                        )
                    await _publish(
                        "thread_updated",
                        client_id=client_id,
                        thread_id=thread_id,
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
        await _publish(
            "thread_updated",
            client_id=body.get("client_id"),
            thread_id=thread_id,
        )

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
        # Long-lived SSE event stream for live multi-tab/-device sync.
        Route("/chat/api/events/stream", events_stream, methods=["GET"]),
        # PIN unlock (public, rate-limited). Trades a short PIN for the
        # bearer token, avoiding the need to paste the long token into
        # browser URLs or prompts.
        Route("/chat/api/pin/status", pin_status, methods=["GET"]),
        Route("/chat/api/pin/unlock", pin_unlock, methods=["POST"]),
        # PIN change (requires bearer auth -- the middleware enforces it).
        # Takes old + new PIN; on success the new PIN is persisted to
        # SQLite and takes effect immediately.
        Route("/chat/api/pin/change", pin_change, methods=["POST"]),
        # Command Center -- account summary strip
        Route("/chat/api/account/summary", account_summary, methods=["GET"]),
        Route("/chat/api/positions", positions, methods=["GET"]),
        Route("/chat/api/pivot/{symbol}", pivot_analysis, methods=["GET"]),
        # Pivot-loop persistent state (SQLite-backed). Claude reads/writes
        # through these endpoints (and the matching MCP tools) so loop
        # state survives restarts + cross-device + multi-thread.
        Route("/chat/api/loops", pivot_loops_list, methods=["GET"]),
        Route("/chat/api/loops", pivot_loops_create, methods=["POST"]),
        Route("/chat/api/loops/{symbol}", pivot_loop_get, methods=["GET"]),
        Route("/chat/api/loops/{symbol}", pivot_loop_patch, methods=["PATCH"]),
        Route("/chat/api/loops/{symbol}", pivot_loop_stop, methods=["DELETE"]),
        Route(
            "/chat/api/loops/{symbol}/cycles",
            pivot_loop_record_cycle,
            methods=["POST"],
        ),
        # Command Center -- watchlists / portfolios CRUD
        Route("/chat/api/watchlists", watchlists_list, methods=["GET"]),
        Route("/chat/api/watchlists", watchlists_create, methods=["POST"]),
        Route("/chat/api/watchlists/{wid}", watchlists_delete, methods=["DELETE"]),
        Route("/chat/api/watchlists/{wid}/stocks", watchlist_stocks_list, methods=["GET"]),
        Route("/chat/api/watchlists/{wid}/stocks", watchlist_stocks_add, methods=["POST"]),
        Route("/chat/api/watchlists/{wid}/stocks/{symbol}",
              watchlist_stocks_update, methods=["PATCH"]),
        Route("/chat/api/watchlists/{wid}/stocks/{symbol}",
              watchlist_stocks_remove, methods=["DELETE"]),
        # Command Center -- single-symbol live quote for price ticker
        Route("/chat/api/market/quote", market_quote, methods=["GET"]),
        # Command Center -- structured analyst research (proxies Anthropic
        # with web_search; prompt prefix cached for cost reduction).
        Route("/chat/api/research/{symbol}", research_symbol, methods=["GET"]),
        # Server-side user prefs (UI state) -- replaces browser localStorage
        # so settings sync across devices.
        Route("/chat/api/prefs", prefs_list, methods=["GET"]),
        Route("/chat/api/prefs", prefs_set, methods=["POST"]),
        Route("/chat/api/prefs/{key}", prefs_delete, methods=["DELETE"]),
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
