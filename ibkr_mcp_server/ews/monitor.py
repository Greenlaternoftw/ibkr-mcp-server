"""EWS monitor — the periodic scan loop (brief Section 4 Step 5).

Server-side equivalent of the brief's browser monitorService. One scan
cycle:

  1. Pull the held positions (real IBKR positions, all managed accounts).
  2. For each, fetch all six signals (signals.fetch_all_signals) — these
     run concurrently across tickers (Promise.all equivalent).
  3. Analyze each with Claude (analyzer.analyze_position), in small
     batches to respect Anthropic rate limits.
  4. Persist every recommendation to the alert feed.
  5. ntfy-push the ones at/above ews_push_min_severity.

The loop is failure-isolated end-to-end: a bad ticker yields a HOLD
fallback, a dead source yields empty signal, and the whole cycle is
wrapped so one exception never kills the loop — it logs and waits for
the next interval (same contract as the portfolio snapshot loop).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from .. import notify
from ..config import settings
from . import analyzer, persistence, signals as signals_mod
from . import severity_at_least

logger = logging.getLogger(__name__)

_ANALYSIS_BATCH = 4  # analyze N positions concurrently (rate-limit friendly)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def _gather_positions(ibkr_client) -> List[Dict[str, Any]]:
    """Distinct STK positions across all managed accounts, deduped by
    symbol (a symbol held in two accounts is scanned once; the alert
    notes the account with the largest position)."""
    try:
        if not ibkr_client.is_connected():
            return []
        raw = await ibkr_client.ib.reqPositionsAsync()
    except Exception as e:
        logger.debug(f"EWS: position fetch failed: {e}")
        return []
    by_sym: Dict[str, Dict[str, Any]] = {}
    for p in raw or []:
        c = getattr(p, "contract", None)
        if c is None or (getattr(c, "secType", "") or "").upper() != "STK":
            continue
        qty = float(getattr(p, "position", 0) or 0)
        if not qty:
            continue
        sym = c.symbol
        prev = by_sym.get(sym)
        if prev is None or abs(qty) > abs(prev["shares"]):
            by_sym[sym] = {
                "symbol": sym,
                "shares": qty,
                "avg_cost": float(getattr(p, "avgCost", 0) or 0),
                "current_price": 0.0,   # filled below if available
                "account": getattr(p, "account", None),
            }
    return list(by_sym.values())


def _should_push(rec: Dict[str, Any]) -> bool:
    return severity_at_least(rec.get("severity", "INFO"),
                             settings.ews_push_min_severity)


def _push_alert(position: Dict[str, Any], rec: Dict[str, Any]) -> None:
    """Fire an ntfy push for a high-severity recommendation (brief §6.1
    properties: title = ticker+action+severity, body = title + first step)."""
    sym = position.get("symbol", "?")
    sev = rec.get("severity", "INFO")
    action = rec.get("action", "WATCH")
    emoji = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "📊", "INFO": "ℹ️"}.get(sev, "")
    steps = rec.get("action_steps") or []
    first = steps[0].get("action", "") if steps and isinstance(steps[0], dict) else ""
    title = f"{emoji} {sym} {action} — {sev}"
    body = f"{rec.get('title', '')}".strip()
    if first:
        body += f"\n→ {first}"
    priority = 5 if sev == "CRITICAL" else 4
    try:
        notify.send(title=title, message=body[:300], priority=priority,
                    tags=["rotating_light"] if sev == "CRITICAL" else ["warning"])
    except Exception as e:
        logger.debug(f"EWS ntfy push failed for {sym}: {e}")


async def run_scan(ibkr_client) -> Dict[str, Any]:
    """Run ONE full EWS scan cycle. Returns a summary dict. Safe to call
    from the loop or the manual scan-now endpoint."""
    store = persistence.get_store()
    scan_id = store.start_scan(_now_iso())
    positions = await _gather_positions(ibkr_client)
    alerts_made = 0
    pushed = 0
    err: Optional[str] = None

    if not positions:
        store.finish_scan(scan_id, finished_at=_now_iso(), positions=0,
                          alerts=0, pushed=0, error="no positions")
        return {"positions": 0, "alerts": 0, "pushed": 0}

    # Build one Anthropic client for the whole scan (prompt-cache reuse).
    client = None
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        except Exception as e:
            err = f"anthropic init: {e}"

    try:
        # Phase 1: fetch all signals concurrently across tickers.
        sig_results = await asyncio.gather(
            *[signals_mod.fetch_all_signals(p["symbol"]) for p in positions],
            return_exceptions=True,
        )
        # Phase 2: analyze in batches to respect rate limits.
        for i in range(0, len(positions), _ANALYSIS_BATCH):
            batch = positions[i:i + _ANALYSIS_BATCH]
            batch_sigs = sig_results[i:i + _ANALYSIS_BATCH]
            recs = await asyncio.gather(
                *[analyzer.analyze_position(
                    pos,
                    s if not isinstance(s, Exception) else {"symbol": pos["symbol"]},
                    client=client)
                  for pos, s in zip(batch, batch_sigs)],
                return_exceptions=True,
            )
            for pos, rec in zip(batch, recs):
                if isinstance(rec, Exception):
                    continue
                do_push = _should_push(rec)
                store.insert_alert(created_at=_now_iso(), symbol=pos["symbol"],
                                   account=pos.get("account"), rec=rec,
                                   pushed=do_push)
                alerts_made += 1
                if do_push:
                    _push_alert(pos, rec)
                    pushed += 1
    except Exception as e:
        logger.exception("EWS scan cycle error")
        err = str(e)

    store.finish_scan(scan_id, finished_at=_now_iso(), positions=len(positions),
                      alerts=alerts_made, pushed=pushed, error=err)
    return {"positions": len(positions), "alerts": alerts_made,
            "pushed": pushed, "error": err}


async def scan_loop(ibkr_client) -> None:
    """Background task: run a scan every ews_scan_interval_minutes.

    Mirrors the portfolio-snapshot loop contract: survives errors, logs,
    retries next interval. A 0 interval means the loop exits immediately
    (manual scan-now still works via run_scan)."""
    interval_min = settings.ews_scan_interval_minutes
    if not settings.ews_enabled or interval_min <= 0:
        logger.info("EWS scan loop disabled (ews_enabled=%s interval=%s)",
                    settings.ews_enabled, interval_min)
        return
    logger.info("EWS scan loop started: every %d min "
                "(push>=%s, UW=%s)", interval_min,
                settings.ews_push_min_severity,
                "on" if settings.uw_api_key else "off")
    # Small initial delay so the daemon finishes connecting first.
    await asyncio.sleep(30)
    while True:
        try:
            summary = await run_scan(ibkr_client)
            logger.info("EWS scan: %s", summary)
        except Exception:
            logger.exception("EWS scan loop tick failed; will retry")
        await asyncio.sleep(interval_min * 60)
