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

    pivot_low: float = 0.0
    pivot_high: float = 0.0
    current_price: float = 0.0

    avg_daily_range: float = 0.0       # mean(daily_high - daily_low)
    avg_close_to_low: float = 0.0      # mean(daily_close - daily_low)
                                       #   = typical rise off the low
    median_close_to_low: float = 0.0   # robust alternative for noisy series

    distance_from_low_pct: float = 0.0    # current vs pivot_low
    distance_from_high_pct: float = 0.0   # current vs pivot_high

    suggested_entry: float = 0.0     # pivot_low × (1 + entry_buffer_pct)
    suggested_target: float = 0.0    # entry + avg_close_to_low
    suggested_stop: float = 0.0      # pivot_low × (1 - stop_buffer_pct)
    risk_reward_ratio: float = 0.0   # (target - entry) / (entry - stop)

    catalysts: List[Dict[str, Any]] = field(default_factory=list)
    blocked_by_catalyst: bool = False
    days_to_next_catalyst: Optional[int] = None

    recommendation: str = ""         # BUY / WAIT / HOLD / SELL / EXIT-CATALYST
    notes: List[str] = field(default_factory=list)


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

    # Avoid division-by-zero on a flat series.
    distance_from_low_pct = (
        (current_price - pivot_low) / pivot_low * 100 if pivot_low > 0 else 0.0
    )
    distance_from_high_pct = (
        (current_price - pivot_high) / pivot_high * 100 if pivot_high > 0 else 0.0
    )

    suggested_entry = pivot_low * (1 + entry_buffer_pct)
    suggested_stop = pivot_low * (1 - stop_buffer_pct)
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

    # Recommendation logic, ordered by precedence:
    if blocked_by_catalyst:
        recommendation = f"EXIT — catalyst within {catalyst_horizon_days}d"
    elif current_price <= suggested_entry:
        recommendation = "BUY — at or below suggested entry"
    elif current_price <= pivot_low * (1 + entry_buffer_pct * 2):
        recommendation = "WAIT — close to entry, monitor for pullback"
    elif current_price >= suggested_target:
        recommendation = "SELL — current price at/above target"
    else:
        recommendation = "HOLD — between entry and target"

    return PivotAnalysis(
        bars_used=len(bars),
        pivot_low=round(pivot_low, 4),
        pivot_high=round(pivot_high, 4),
        current_price=round(current_price, 4),
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
