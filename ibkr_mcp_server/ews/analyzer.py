"""EWS AI analyzer — position + signals -> structured recommendation.

Brief Section 4 Step 4, adapted: the brief calls Claude from the browser
(then its own Section 9.1 forbids that). We call Anthropic server-side
with the daemon's existing key, exactly like chat/agent.py, so the key
never leaves the VPS.

Output contract (brief Step 4), validated and defaulted:
  action          : BUY | SELL | HOLD | HEDGE | TRIM | WATCH
  severity        : CRITICAL | HIGH | MEDIUM | INFO
  title           : <= 60 chars
  summary         : 2-3 sentences referencing specific signals
  signals_detected: [str]
  action_steps    : [{step:int, action:str, rationale:str}]  (3 items)
  price_targets   : {"30d","60d","90d","eoy"}  (strings)
  trigger_sources : subset of NEWS, OPTIONS_FLOW, DARK_POOL,
                    SHORT_INTEREST, EDGAR, IPO_CALENDAR

On ANY failure (no key, API error, unparseable JSON) we return a
conservative HOLD/INFO recommendation rather than raising — the scan
loop must never crash on one bad ticker.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from ..config import settings

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"BUY", "SELL", "HOLD", "HEDGE", "TRIM", "WATCH"}
_VALID_SEVERITY = {"CRITICAL", "HIGH", "MEDIUM", "INFO"}

_SYSTEM_PROMPT = (
    "You are a portfolio early-warning analyst for a retail investor. "
    "You receive ONE position (ticker, shares, cost basis, current price, "
    "P&L) plus freshly-fetched market signals (SEC EDGAR filings, options "
    "flow, short interest, dark-pool prints, news sentiment). Assess "
    "whether the holder should act. Weight hard catalysts (S-1 secondary "
    "offerings, lock-up waivers, insider selling via Form 4, unusual put "
    "flow, short-interest spikes) far above routine news. Be specific and "
    "cite the actual signals you saw. Tailor action steps to THIS holder's "
    "exact share count and cost basis. "
    "Respond with ONLY valid JSON — no markdown fences, no preamble — "
    "matching exactly this shape: "
    '{"action": "BUY|SELL|HOLD|HEDGE|TRIM|WATCH", '
    '"severity": "CRITICAL|HIGH|MEDIUM|INFO", '
    '"title": "<=60 chars", '
    '"summary": "2-3 sentences citing specific signals", '
    '"signals_detected": ["..."], '
    '"action_steps": [{"step": 1, "action": "...", "rationale": "..."}], '
    '"price_targets": {"30d": "$X", "60d": "$X", "90d": "$X", "eoy": "$X"}, '
    '"trigger_sources": ["NEWS|OPTIONS_FLOW|DARK_POOL|SHORT_INTEREST|EDGAR|IPO_CALENDAR"]}. '
    "Severity guide: CRITICAL = act now (S-1 detected, -15% session, margin "
    "risk); HIGH = urgent (unusual put spike, PE/insider selling, lock-up "
    "waiver); MEDIUM = review (short interest rising, dark-pool below-market); "
    "INFO = routine. Default to HOLD/INFO when signals are thin."
)


def _hold_fallback(symbol: str, reason: str) -> Dict[str, Any]:
    return {
        "action": "HOLD",
        "severity": "INFO",
        "title": f"{symbol}: no actionable signal",
        "summary": f"No strong signals detected ({reason}). Holding; will "
                   "re-evaluate next scan.",
        "signals_detected": [],
        "action_steps": [
            {"step": 1, "action": "No action required",
             "rationale": reason},
        ],
        "price_targets": {"30d": "", "60d": "", "90d": "", "eoy": ""},
        "trigger_sources": [],
        "_fallback": True,
    }


def _coerce(rec: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Validate + normalize a parsed recommendation, filling gaps."""
    action = str(rec.get("action", "HOLD")).upper()
    if action not in _VALID_ACTIONS:
        action = "HOLD"
    severity = str(rec.get("severity", "INFO")).upper()
    if severity not in _VALID_SEVERITY:
        severity = "INFO"
    title = str(rec.get("title") or f"{symbol} update")[:60]
    steps = rec.get("action_steps")
    if not isinstance(steps, list) or not steps:
        steps = [{"step": 1, "action": "Review position", "rationale": ""}]
    pt = rec.get("price_targets")
    if not isinstance(pt, dict):
        pt = {}
    for k in ("30d", "60d", "90d", "eoy"):
        pt.setdefault(k, "")
    srcs = rec.get("trigger_sources")
    if not isinstance(srcs, list):
        srcs = []
    sd = rec.get("signals_detected")
    if not isinstance(sd, list):
        sd = []
    return {
        "action": action,
        "severity": severity,
        "title": title,
        "summary": str(rec.get("summary") or ""),
        "signals_detected": [str(s) for s in sd][:12],
        "action_steps": steps[:5],
        "price_targets": {k: str(pt.get(k, "")) for k in ("30d", "60d", "90d", "eoy")},
        "trigger_sources": [str(s).upper() for s in srcs][:6],
        "_fallback": False,
    }


def _strip_json(text: str) -> str:
    """Strip accidental markdown fences / 'json' prefixes (brief Step 4)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t[3:]
        t = t.strip()
    if t.lower().startswith("json"):
        t = t[4:].strip()
    # Grab the outermost JSON object if there's trailing prose.
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return t


async def analyze_position(position: Dict[str, Any],
                           signals: Dict[str, Any],
                           client: Optional[Any] = None) -> Dict[str, Any]:
    """Ask Claude for a recommendation on one position given its signals.

    position: {symbol, shares, avg_cost, current_price, unrealized_pnl}
    signals : output of signals.fetch_all_signals
    client  : optional pre-built AsyncAnthropic (reused across a scan to
              avoid per-call construction). Built lazily if omitted.
    """
    symbol = position.get("symbol", "?")
    if not settings.anthropic_api_key:
        return _hold_fallback(symbol, "no Anthropic key configured")

    try:
        if client is None:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        user_payload = {
            "position": position,
            "signals": {
                "edgar": signals.get("edgar", {}),
                "unusual_whales": signals.get("uw", {}),
                "news": signals.get("news", {}),
                "uw_available": signals.get("uw_available", False),
            },
        }
        resp = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=900,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                # Cache the (stable) system prompt across the scan's many
                # per-ticker calls -- big token saving on a 13+ position scan.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": "Analyze this position and its signals:\n"
                           + json.dumps(user_payload, default=str),
            }],
        )
        # Concatenate text blocks.
        text = "".join(
            b.text for b in resp.content
            if getattr(b, "type", None) == "text"
        )
        parsed = json.loads(_strip_json(text))
        if not isinstance(parsed, dict):
            return _hold_fallback(symbol, "non-object JSON")
        return _coerce(parsed, symbol)
    except json.JSONDecodeError:
        logger.debug(f"EWS analyzer: unparseable JSON for {symbol}")
        return _hold_fallback(symbol, "unparseable model response")
    except Exception as e:
        logger.debug(f"EWS analyzer failed for {symbol}: {e}")
        return _hold_fallback(symbol, f"analyzer error: {e}")
