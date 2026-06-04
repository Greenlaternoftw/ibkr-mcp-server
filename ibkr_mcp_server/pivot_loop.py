"""Pivot Loop autonomous engine -- Phase B.

The dashboard creates a loop record in SQLite (Phase A); this module is
the daemon-side ticker that drives it autonomously. Every
``DEFAULT_TICK_SECONDS`` (60s during RTH) the engine:

  1. Reads the loop's current state from SQLite (single source of truth).
  2. Re-analyses the pivot via :mod:`pivot` (bars, trend, catalyst block).
  3. Decides the next action based on state + analysis (pure function,
     :func:`decide_next_action`).
  4. Executes the action (place / cancel / record cycle / auto-stop)
     and writes the new state back to SQLite.
  5. ntfy-pushes anything operator-visible (entry filled, exit filled,
     catalyst-driven close, auto-stop).

The pure-decision split (:func:`decide_next_action`) means the policy
is unit-testable with synthetic state + analysis fixtures -- no IBKR
mocking needed for the logic tests.

The engine is OPERATOR PRE-AUTHORIZED at loop-creation time: orders
go out with ``confirm=True`` and skip the destructive-tool gate, the
same as the dashboard quick-action buttons. The gate's protections
are replaced here by:

  * max_drawdown_pct (default 50%) -- auto-stops the loop once
    cumulative loss exceeds the threshold.
  * Catalyst horizon -- refuses new entries (and force-exits open
    positions) within ``catalyst_horizon_days`` of earnings / ex-div.
  * Trend strength -- refuses new entries in moderate/strong
    downtrends (the falling-knife guard).
  * 60s tick floor -- the engine can't spam orders faster than
    once a minute per symbol.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


# Tick cadence. Operator-facing "1m during RTH" per request. The OTH
# branch sleeps longer because nothing changes meaningfully outside RTH
# (we still tick to catch a forced catalyst exit on Monday-morning
# news, but every 5 minutes is plenty).
DEFAULT_TICK_SECONDS_RTH = 60
DEFAULT_TICK_SECONDS_OTH = 300

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------
# Market regime detection
# ---------------------------------------------------------------------

def is_regular_trading_hours(now_utc: Optional[dt.datetime] = None) -> bool:
    """True if `now_utc` (default: now) falls inside US equity RTH:
    weekday 9:30-16:00 ET. Holiday calendar is intentionally NOT modeled
    here -- IBKR rejects MKT/LMT IOC on closed days, so we let that
    error bubble up rather than maintaining a holiday list.
    """
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    et_now = now.astimezone(ET)
    if et_now.weekday() >= 5:
        return False
    minutes = et_now.hour * 60 + et_now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def current_tick_interval(now_utc: Optional[dt.datetime] = None) -> int:
    """Pick the tick cadence based on market regime."""
    return DEFAULT_TICK_SECONDS_RTH if is_regular_trading_hours(now_utc) \
        else DEFAULT_TICK_SECONDS_OTH


# ---------------------------------------------------------------------
# Broader-market regime (cached for 1 hour)
# ---------------------------------------------------------------------
#
# Phase E filter: refuse pivot entries when SPY is in a "risk-off"
# regime (SMA-50 trending down + ADX high, per the existing
# regime.py rules). Per-tick we don't want a fresh 250-day SPY fetch
# (rate limits + bandwidth), so cache for 1 hour. Regime doesn't
# flip minute-to-minute.

_REGIME_CACHE: Dict[str, Any] = {"fetched_at": None, "enabled": None}
_REGIME_CACHE_TTL_SECONDS = 3600
_REGIME_INDEX_SYMBOL = "SPY"


async def get_market_regime_enabled(client) -> Optional[bool]:
    """Return True/False for SPY's regime; None if the fetch failed.

    Cached for 1h so a tick storm doesn't hammer IBKR's historical
    data endpoint.  Caller is `client` (an IBKRClient instance --
    typed as Any to avoid the circular import).
    """
    now = dt.datetime.now(dt.timezone.utc)
    last = _REGIME_CACHE["fetched_at"]
    if last and (now - last).total_seconds() < _REGIME_CACHE_TTL_SECONDS:
        return _REGIME_CACHE["enabled"]
    try:
        bars = await client.get_historical_bars(
            _REGIME_INDEX_SYMBOL, lookback_days=250
        )
        from .regime import check_regime_from_bars
        result = check_regime_from_bars(_REGIME_INDEX_SYMBOL, bars)
        enabled = bool(result.get("enabled"))
    except Exception as e:
        logger.warning(f"pivot-loop regime fetch failed: {e}")
        enabled = None
    _REGIME_CACHE["fetched_at"] = now
    _REGIME_CACHE["enabled"] = enabled
    return enabled


def clear_regime_cache() -> None:
    """Wipe the cache. Used by tests + a future force-refresh button."""
    _REGIME_CACHE["fetched_at"] = None
    _REGIME_CACHE["enabled"] = None


# ---------------------------------------------------------------------
# Decision policy (pure -- no I/O)
# ---------------------------------------------------------------------

@dataclass
class Decision:
    """What the engine should do this tick.

    `action` is one of:
      - "no_op"          → nothing to do; sleep until next tick
      - "place_entry"    → fresh entry: BUY LMT IOC at the analyzed entry
      - "monitor_entry"  → entry_pending; check whether it filled
      - "place_oca"      → entry filled; need to attach OCA protection
      - "monitor_holding"→ holding; OCA children handle the exit naturally
      - "force_exit"     → catalyst within horizon: close manually NOW
      - "record_cycle"   → position closed (target/stop fired); ledger it
      - "auto_stop"      → drawdown hit / 3 consecutive losses; end loop
    """
    action: str
    reason: str = ""
    extra: Dict[str, Any] = None  # type: ignore[assignment]


# A symbol can hold at most these many consecutive losing cycles before
# the engine auto-stops. Operator-tunable per-loop in a follow-up.
MAX_CONSECUTIVE_LOSSES = 3


def decide_next_action(
    loop: Dict[str, Any],
    analysis: Any,
    *,
    has_open_position: bool,
    last_3_cycles_losses: int,
) -> Decision:
    """Pure decision function. Inputs are the loop state row (dict from
    SQLite), the latest PivotAnalysis, and two summarised IBKR signals
    (open position bool, recent loss streak). Returns what the engine
    should do this tick. Side-effect-free.
    """
    status = loop["status"]

    # ---- HARD STOPS first (apply regardless of position state) ------

    # Drawdown stop: cumulative loss exceeds max_drawdown_pct of initial.
    if loop["cumulative_realized"] < 0:
        max_loss = loop["initial_capital"] * loop["max_drawdown_pct"] / 100.0
        if abs(loop["cumulative_realized"]) >= max_loss:
            return Decision(
                action="auto_stop",
                reason=f"drawdown ${loop['cumulative_realized']:.2f} ≥ ${max_loss:.2f} threshold",
            )

    # Losing-streak stop: too many losses in a row → algorithm review.
    if last_3_cycles_losses >= MAX_CONSECUTIVE_LOSSES:
        return Decision(
            action="auto_stop",
            reason=f"{MAX_CONSECUTIVE_LOSSES} consecutive losing cycles",
        )

    # ---- State-driven decisions ------------------------------------

    if status == "waiting":
        if has_open_position:
            # IBKR shows a position we don't know about -- reconcile by
            # recording the open-from-elsewhere position as cycle start.
            # For safety, do NOT auto-enter; flag for operator review.
            return Decision(
                action="no_op",
                reason="position exists for symbol but loop is in 'waiting' state -- skip; operator may have entered manually",
            )
        # Catalyst block always wins.
        if analysis.blocked_by_catalyst:
            return Decision(
                action="no_op",
                reason=f"catalyst block ({analysis.days_to_next_catalyst}d to next event)",
            )
        # Falling-knife guard.
        if (analysis.trend_direction == "down"
                and analysis.trend_strength in ("moderate", "strong")):
            return Decision(
                action="no_op",
                reason=f"trend is {analysis.trend_strength} down ({analysis.trend_pct_change}%)",
            )
        # Broader-market regime gate (Phase E). None means we couldn't
        # fetch SPY -- skip the gate rather than block trading entirely.
        # Explicit False means SPY is risk-off; don't fight the tape.
        if getattr(analysis, "market_regime_enabled", None) is False:
            return Decision(
                action="no_op",
                reason="SPY market regime is risk-off (trend/ADX gate failed)",
            )
        # Realized-vol "IV proxy" gate (Phase D). vol_ok=False means
        # recent realized vol has expanded vs baseline -- likely an
        # event being priced in even without a named catalyst.
        if getattr(analysis, "vol_ok", None) is False:
            vr = getattr(analysis, "vol_ratio", None)
            return Decision(
                action="no_op",
                reason=f"realized vol expanding (ratio {vr:.2f}× baseline)" if vr is not None
                       else "realized vol expanding above threshold",
            )
        # Volume confirmation gate (Phase C). None means bars don't carry
        # volume (test fixtures or odd ib_async build); skip. False means
        # recent volume is below the threshold -- low-conviction pivot.
        if getattr(analysis, "volume_ok", None) is False:
            vr = getattr(analysis, "volume_ratio", None)
            return Decision(
                action="no_op",
                reason=f"volume confirmation failed (ratio {vr:.2f}× avg)" if vr is not None
                       else "volume confirmation failed",
            )
        # Price must be at or near the suggested entry.
        if analysis.current_price > analysis.suggested_entry * 1.005:
            return Decision(
                action="no_op",
                reason=(
                    f"price ${analysis.current_price:.2f} > entry "
                    f"${analysis.suggested_entry:.2f} × 1.005"
                ),
            )
        return Decision(
            action="place_entry",
            reason=f"conditions met at ${analysis.current_price:.2f}",
            extra={"entry_target_price": analysis.suggested_entry,
                   "stop_price": analysis.suggested_stop,
                   "target_price": analysis.suggested_target},
        )

    if status == "entry_pending":
        if has_open_position:
            return Decision(action="place_oca", reason="entry filled; attach protection",
                            extra={"stop_price": analysis.suggested_stop,
                                   "target_price": analysis.suggested_target})
        # IOC orders cancel on no-fill. If still entry_pending after a
        # tick and no position, the IOC didn't fill -- step back to waiting.
        return Decision(
            action="no_op",
            reason="entry IOC presumed cancelled; reverting to waiting on next tick",
            extra={"revert_to_waiting": True},
        )

    if status == "holding":
        if not has_open_position:
            # Position closed (OCA fired). Record the cycle.
            return Decision(
                action="record_cycle",
                reason="position closed; OCA child fill detected",
            )
        # Open position + catalyst inbound → force exit before the event.
        if analysis.blocked_by_catalyst:
            return Decision(
                action="force_exit",
                reason=f"catalyst within {analysis.days_to_next_catalyst}d; close before event",
            )
        return Decision(action="monitor_holding", reason="OCA protection in force")

    # exit_pending / paused / stopped: nothing for the engine to do.
    return Decision(action="no_op", reason=f"status={status} -- engine idle")
