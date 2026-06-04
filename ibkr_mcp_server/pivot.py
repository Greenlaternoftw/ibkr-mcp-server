"""Pivot-loop analysis.

Determines entry/exit levels for a mean-reversion "buy the pivot low,
sell at the average rise" strategy, with catalyst awareness so positions
can be flagged for exit before earnings / ex-dividend / etc.

Pure logic only -- no IBKR or HTTP. The route in `chat/routes.py` pulls
historical bars + catalyst data and feeds them in.

Design notes:
- "Pivot low" = minimum daily low across the lookback window. Robust to
  outliers vs daily close minimums because intraday spikes are what
  mean-reversion buyers actually pay.
- "Average rise" = mean(daily_close - daily_low) over the window. This is
  the typical bounce off the day's low, which becomes the profit target
  delta above the entry.
- Catalyst block: if any catalyst falls within ``catalyst_horizon_days``
  of today, the recommendation is EXIT regardless of price -- a 1.5%
  profit target is not worth holding through an earnings call that
  could move the stock ±5%.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class PivotAnalysis:
    """Output of :func:`analyze_pivot_loop`. JSON-serializable via asdict()."""

    symbol: str = ""
    lookback_days: int = 0
    bars_used: int = 0

    pivot_low: float = 0.0           # raw min(low) over the window
    pivot_high: float = 0.0
    current_price: float = 0.0

    # Trend diagnosis -- drives the effective_low adjustment below.
    trend_pct_change: float = 0.0    # % change from first close to last close
    trend_direction: str = "flat"    # "up" / "down" / "flat"
    trend_strength: str = "weak"     # "weak" / "moderate" / "strong"

    # The floor reference USED for entry/stop math. Differs from
    # pivot_low when the window is trending -- uptrends shift to the
    # trailing 3d low; downtrends project the slope forward 1-2 days.
    effective_low: float = 0.0
    effective_low_source: str = ""

    avg_daily_range: float = 0.0       # mean(daily_high - daily_low)
    avg_close_to_low: float = 0.0      # mean(daily_close - daily_low)
                                       #   = typical rise off the low
    median_close_to_low: float = 0.0   # robust alternative for noisy series

    distance_from_low_pct: float = 0.0    # current vs effective_low
    distance_from_high_pct: float = 0.0   # current vs pivot_high

    suggested_entry: float = 0.0     # effective_low × (1 + entry_buffer_pct)
    suggested_target: float = 0.0    # entry + median rise
    suggested_stop: float = 0.0      # effective_low × (1 - stop_buffer_pct)
    risk_reward_ratio: float = 0.0   # (target - entry) / (entry - stop)

    catalysts: List[Dict[str, Any]] = field(default_factory=list)
    blocked_by_catalyst: bool = False
    days_to_next_catalyst: Optional[int] = None

    recommendation: str = ""         # BUY / WAIT / HOLD / SELL / EXIT-CATALYST
    notes: List[str] = field(default_factory=list)


def _diagnose_trend(bars) -> Dict[str, Any]:
    """Estimate trend direction + strength over the window.

    Uses first-close → last-close % change (simpler and more honest than
    a linreg slope for short windows; the user only cares whether the
    stock is generally rising, falling, or flat).

    Thresholds chosen empirically:
      - ±2% over window  → directional (up / down)
      - ±5% over window  → strong directional move
      - everything else  → flat

    Returns dict shaped:
      {direction: "up" | "down" | "flat",
       strength:  "weak" | "moderate" | "strong",
       pct_change: float,
       slope_per_day: float  -- $ change per bar; used to project the low}
    """
    closes = bars["close"].tolist()
    if len(closes) < 2 or closes[0] == 0:
        return {"direction": "flat", "strength": "weak",
                "pct_change": 0.0, "slope_per_day": 0.0}
    first, last = float(closes[0]), float(closes[-1])
    pct = (last - first) / first * 100.0
    slope_per_day = (last - first) / max(1, len(closes) - 1)
    abs_pct = abs(pct)
    direction = "up" if pct > 2.0 else "down" if pct < -2.0 else "flat"
    strength = (
        "strong" if abs_pct > 5.0
        else "moderate" if abs_pct > 2.0
        else "weak"
    )
    return {
        "direction": direction,
        "strength": strength,
        "pct_change": round(pct, 2),
        "slope_per_day": round(slope_per_day, 4),
    }


def _compute_effective_low(bars, pivot_low: float, trend: Dict[str, Any]) -> tuple:
    """Pick the floor reference appropriate for current trend.

    Returns ``(effective_low, source_description)``:
      - Uptrend:  trailing 3-day low (older lows are stale -- price moved
                  past them).
      - Downtrend: pivot_low + 1.5 days of slope drift (today's low isn't
                   tomorrow's; project where the next low likely lands).
      - Flat: raw pivot_low.

    For uptrend with only 1-2 bars (lookback=3 edge case) we fall back to
    raw pivot_low rather than slice an empty tail.
    """
    if trend["direction"] == "up" and len(bars) >= 3:
        tail_low = float(bars["low"].tail(3).min())
        return tail_low, f"trailing 3d low (uptrend +{trend['pct_change']}%)"
    if trend["direction"] == "down":
        # Project the floor 1.5 days forward along the slope. slope is
        # NEGATIVE in a downtrend, so the projected low is BELOW pivot_low
        # -- the algo expects further weakness.
        projected = pivot_low + trend["slope_per_day"] * 1.5
        return projected, (
            f"pivot + 1.5d projected drift "
            f"(downtrend {trend['pct_change']}%, slope ${trend['slope_per_day']:.2f}/d)"
        )
    return pivot_low, "window low (flat trend)"


def analyze_pivot_loop(
    bars,
    catalysts: Optional[List[Dict[str, Any]]] = None,
    *,
    entry_buffer_pct: float = 0.005,
    stop_buffer_pct: float = 0.03,
    catalyst_horizon_days: int = 2,
) -> PivotAnalysis:
    """Compute the pivot-loop analysis from a bars DataFrame + catalyst list.

    Args:
      bars: pandas DataFrame with columns ``[high, low, close]``, indexed
        oldest-first.  Caller is responsible for trimming to the user's
        chosen lookback window.
      catalysts: list of ``{type, date, days_away, description?}`` dicts.
        ``days_away`` is the number of calendar days from today
        (negative for past events; only future ones matter for the
        block check).  Pass None / empty list if the operator hasn't
        enabled the catalyst feed.
      entry_buffer_pct: how far above the pivot low to set the suggested
        entry. Default 0.5% -- gives a slight margin so we don't insist
        on the absolute bottom tick.
      stop_buffer_pct: how far below the pivot low to set the hard stop.
        Default 3% -- if the floor breaks decisively, the thesis is wrong.
      catalyst_horizon_days: any catalyst within this many days triggers
        an EXIT recommendation. Default 2 -- the operator wants to be
        out *before* the event, not on the morning of.

    Returns: :class:`PivotAnalysis` populated except for ``symbol`` /
    ``lookback_days`` (filled by the caller after the fact).
    """
    if bars is None or len(bars) < 2:
        raise ValueError("need at least 2 bars to analyze")
    required = {"high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")

    pivot_low = float(bars["low"].min())
    pivot_high = float(bars["high"].max())
    current_price = float(bars["close"].iloc[-1])

    daily_range = bars["high"] - bars["low"]
    close_to_low = bars["close"] - bars["low"]
    avg_daily_range = float(daily_range.mean())
    avg_close_to_low = float(close_to_low.mean())
    median_close_to_low = float(close_to_low.median())

    # Trend diagnosis + effective floor selection. This is the "auto-
    # adjust from the previous days if the price is trending" logic:
    # uptrends pick a trailing low instead of a stale window-min;
    # downtrends project the floor forward instead of trusting today's.
    trend = _diagnose_trend(bars)
    effective_low, effective_low_source = _compute_effective_low(
        bars, pivot_low, trend
    )

    # Distance measurements use the EFFECTIVE floor so they reflect
    # current-trend reality, not the stale window min.
    distance_from_low_pct = (
        (current_price - effective_low) / effective_low * 100
        if effective_low > 0 else 0.0
    )
    distance_from_high_pct = (
        (current_price - pivot_high) / pivot_high * 100 if pivot_high > 0 else 0.0
    )

    suggested_entry = effective_low * (1 + entry_buffer_pct)
    suggested_stop = effective_low * (1 - stop_buffer_pct)
    # Use the median rise (more robust to a single explosive bounce in
    # the window) as the target delta, with the mean as a sanity check.
    target_delta = max(median_close_to_low, avg_close_to_low * 0.5)
    suggested_target = suggested_entry + target_delta

    risk = suggested_entry - suggested_stop
    reward = suggested_target - suggested_entry
    risk_reward_ratio = round(reward / risk, 2) if risk > 0 else 0.0

    # Catalyst block: anything in the next `catalyst_horizon_days` days
    # forces EXIT regardless of price level.
    upcoming = [
        c for c in (catalysts or [])
        if c.get("days_away") is not None and c["days_away"] >= 0
    ]
    upcoming.sort(key=lambda c: c["days_away"])
    days_to_next = upcoming[0]["days_away"] if upcoming else None
    blocking = [c for c in upcoming if c["days_away"] <= catalyst_horizon_days]
    blocked_by_catalyst = len(blocking) > 0

    notes: List[str] = []
    for c in blocking:
        notes.append(
            f"⚠️ {c['type']} on {c['date']} ({c['days_away']}d away) — "
            "exit before this"
        )
    # Trend annotation -- always surface so the operator sees what
    # adjustment the algo made.
    if trend["direction"] != "flat":
        arrow = "↑" if trend["direction"] == "up" else "↓"
        notes.append(
            f"{arrow} {trend['strength']} {trend['direction']}trend "
            f"({trend['pct_change']:+.2f}% over window) — "
            f"floor reference: {effective_low_source}"
        )

    # Recommendation logic, ordered by precedence. Trend awareness:
    # in a confirmed downtrend, refuse new entries even at the entry
    # level -- the floor itself is in motion. In a strong uptrend
    # already past target, recommendation flips to "TRENDING — entry
    # missed" so the operator doesn't chase.
    if blocked_by_catalyst:
        recommendation = f"EXIT — catalyst within {catalyst_horizon_days}d"
    elif trend["direction"] == "down" and trend["strength"] in ("moderate", "strong"):
        # "Don't catch a falling knife." Even if price hits entry, the
        # projected floor is below entry, so the thesis hasn't
        # stabilized yet.
        recommendation = (
            f"WAIT — {trend['strength']} downtrend; effective floor not "
            "yet stabilized. Re-check after a green close."
        )
    elif current_price <= suggested_entry:
        recommendation = "BUY — at or below suggested entry"
    elif current_price <= effective_low * (1 + entry_buffer_pct * 2):
        recommendation = "WAIT — close to entry, monitor for pullback"
    elif current_price >= suggested_target:
        if trend["direction"] == "up" and trend["strength"] == "strong":
            recommendation = (
                "TRENDING — entry missed; price ran past target on strong "
                "uptrend. Wait for next pullback (next pivot will reset)."
            )
        else:
            recommendation = "SELL — current price at/above target"
    else:
        recommendation = "HOLD — between entry and target"

    return PivotAnalysis(
        bars_used=len(bars),
        pivot_low=round(pivot_low, 4),
        pivot_high=round(pivot_high, 4),
        current_price=round(current_price, 4),
        trend_pct_change=trend["pct_change"],
        trend_direction=trend["direction"],
        trend_strength=trend["strength"],
        effective_low=round(effective_low, 4),
        effective_low_source=effective_low_source,
        avg_daily_range=round(avg_daily_range, 4),
        avg_close_to_low=round(avg_close_to_low, 4),
        median_close_to_low=round(median_close_to_low, 4),
        distance_from_low_pct=round(distance_from_low_pct, 2),
        distance_from_high_pct=round(distance_from_high_pct, 2),
        suggested_entry=round(suggested_entry, 2),
        suggested_target=round(suggested_target, 2),
        suggested_stop=round(suggested_stop, 2),
        risk_reward_ratio=risk_reward_ratio,
        catalysts=upcoming,
        blocked_by_catalyst=blocked_by_catalyst,
        days_to_next_catalyst=days_to_next,
        recommendation=recommendation,
        notes=notes,
    )


def to_json_dict(analysis: PivotAnalysis) -> Dict[str, Any]:
    """asdict() with symbol + lookback_days slots ready for the route to fill."""
    return asdict(analysis)
