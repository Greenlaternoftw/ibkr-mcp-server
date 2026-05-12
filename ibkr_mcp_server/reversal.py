"""Reversal signal detection and tranched entry.

Two layers of logic in this module:

  1. Pure signal detectors that operate on OHLCV bars (`signal_*` and
     `count_signals`). Five signals from the Layer 3 spec — RSI divergence,
     RSI > 30 cross, MACD bullish crossover, higher-low pivot confirmation,
     and volume surge. Used to compute a 0-5 signal count and a recommended
     tranche number.

  2. A `TrancheState` state machine that tracks where a per-symbol entry plan
     is and what the next action should be. Pure given input bars + previous
     state; no IBKR calls. Persisted via the same state-file helper pattern
     as `regime.py`.

The actual hourly loop that wires this to IBKR lives on `IBKRClient` (a
throwaway asyncio task per symbol); Layer 5's daemon will replace it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta


DEFAULT_STATE_PATH = Path.home() / ".ibkr-mcp-reversal-state.json"


# --- signal detectors -------------------------------------------------------


def signal_rsi_divergence(bars: pd.DataFrame, window: int = 20) -> bool:
    """Bullish RSI divergence: within the last `window` days, price made a
    lower low than the previous `window` days, but RSI at that low was higher
    than RSI at the previous low.
    """
    if len(bars) < 2 * window + 14:
        return False
    close = bars["close"]
    rsi = ta.rsi(close, length=14)
    if rsi is None or rsi.isna().all():
        return False

    earlier = close.iloc[-2 * window : -window]
    recent = close.iloc[-window:]

    price_lower_low = recent.min() < earlier.min()
    rsi_at_recent_low = float(rsi.loc[recent.idxmin()])
    rsi_at_earlier_low = float(rsi.loc[earlier.idxmin()])
    return bool(price_lower_low and rsi_at_recent_low > rsi_at_earlier_low)


def signal_rsi_above_30(bars: pd.DataFrame, lookback: int = 3) -> bool:
    """RSI(14) crossed above 30 within the last `lookback` days."""
    if len(bars) < lookback + 14:
        return False
    rsi = ta.rsi(bars["close"], length=14)
    window = rsi.iloc[-(lookback + 1):]
    was_below = (window.iloc[:-1] < 30).any()
    now_above = window.iloc[-1] > 30
    return bool(was_below and now_above)


def signal_macd_crossover(bars: pd.DataFrame, lookback: int = 3) -> bool:
    """MACD line crossed above signal line within the last `lookback` days."""
    if len(bars) < 35 + lookback:
        return False
    macd_df = ta.macd(bars["close"])
    if macd_df is None:
        return False
    macd_col = next(
        c for c in macd_df.columns
        if c.startswith("MACD_") and not c.startswith(("MACDh_", "MACDs_"))
    )
    signal_col = next(c for c in macd_df.columns if c.startswith("MACDs_"))
    macd = macd_df[macd_col]
    sig = macd_df[signal_col]

    window_macd = macd.iloc[-(lookback + 1):]
    window_sig = sig.iloc[-(lookback + 1):]
    was_below = (window_macd.iloc[:-1] <= window_sig.iloc[:-1]).any()
    now_above = window_macd.iloc[-1] > window_sig.iloc[-1]
    return bool(was_below and now_above)


def signal_higher_low(bars: pd.DataFrame, pivot_radius: int = 2) -> bool:
    """Higher low confirmed: using 5-bar swing-low pivots (bar that's the
    lowest within ±`pivot_radius` neighbours), the most recent pivot low is
    higher than the previous one.

    Pivot rule: `lows[i]` is <= both neighbour windows AND strictly lower than
    at least one bar in each side. This tolerates ties on the immediate
    neighbour (common in low-volatility data) while rejecting flat zones.
    Consecutive pivots within `pivot_radius` bars of each other are
    de-duplicated.
    """
    lows = bars["low"].to_numpy()
    if len(lows) < 4 * pivot_radius + 2:
        return False

    pivots: list[int] = []
    for i in range(pivot_radius, len(lows) - pivot_radius):
        left = lows[i - pivot_radius : i]
        right = lows[i + 1 : i + pivot_radius + 1]
        is_min = lows[i] <= left.min() and lows[i] <= right.min()
        has_higher_on_each_side = lows[i] < left.max() and lows[i] < right.max()
        if is_min and has_higher_on_each_side:
            if pivots and i - pivots[-1] < pivot_radius:
                continue
            pivots.append(i)

    if len(pivots) < 2:
        return False
    return bool(lows[pivots[-1]] > lows[pivots[-2]])


def signal_volume_surge(
    bars: pd.DataFrame,
    lookback: int = 20,
    threshold: float = 1.5,
) -> bool:
    """The most recent up-day's volume > `threshold` × the trailing
    `lookback`-day average volume.
    """
    if len(bars) < lookback + 2:
        return False
    closes = bars["close"]
    volumes = bars["volume"]
    # Walk back from the latest bar to the most recent up-day, then check vol.
    for i in range(len(bars) - 1, lookback, -1):
        if closes.iloc[i] > closes.iloc[i - 1]:
            avg_vol = volumes.iloc[i - lookback : i].mean()
            return bool(volumes.iloc[i] > threshold * avg_vol)
    return False


def count_signals(bars: pd.DataFrame) -> dict[str, bool]:
    return {
        "rsi_divergence": signal_rsi_divergence(bars),
        "rsi_above_30": signal_rsi_above_30(bars),
        "macd_crossover": signal_macd_crossover(bars),
        "higher_low": signal_higher_low(bars),
        "volume_surge": signal_volume_surge(bars),
    }


# --- tranche state machine -------------------------------------------------


class ReversalStatus(str, Enum):
    WATCHING = "WATCHING"            # no tranches filled yet
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # at least one tranche filled
    STALLED = "STALLED"              # signals dropped, awaiting recovery
    COMPLETE = "COMPLETE"            # all tranches filled
    CANCELLED = "CANCELLED"          # operator-cancelled
    LIQUIDATED = "LIQUIDATED"        # operator-liquidated
    ABORTED = "ABORTED"              # stall timeout exceeded


@dataclass
class ReversalConfig:
    tranche_count: int = 3
    tranche_sizing: str = "equal"            # or "weighted"
    min_signals_for_entry: int = 3
    signals_per_tranche: int = 1
    signal_window_days: int = 3
    stall_timeout_days: int = 10
    protective_stop_atr_multiple: float = 2.0


@dataclass
class FilledTranche:
    index: int                # 1-based
    target_dollars: float
    shares: int
    fill_price: float
    filled_at: str            # ISO date


@dataclass
class ReversalState:
    """Per-symbol state for an active reversal entry plan."""

    symbol: str
    total_dollars: float
    config: ReversalConfig
    status: ReversalStatus = ReversalStatus.WATCHING

    # Most recent signal evaluation
    last_signal_count: int = 0
    last_signal_dict: dict[str, bool] = field(default_factory=dict)
    consecutive_days_at_threshold: int = 0
    last_check_date: str = ""

    # Progress
    filled_tranches: list[FilledTranche] = field(default_factory=list)
    started_at: str = ""
    last_action_at: str = ""
    protective_stop_order_id: int | None = None

    def tranche_dollar_amount(self, index_1based: int) -> float:
        """Dollar amount for tranche #index (1-based)."""
        n = self.config.tranche_count
        if self.config.tranche_sizing == "weighted":
            # Layer 3 spec: 20% / 30% / 50% for n=3
            if n == 3:
                weights = [0.20, 0.30, 0.50]
            else:
                # Linear ramp for other tranche counts
                raw = np.arange(1, n + 1, dtype=float)
                weights = (raw / raw.sum()).tolist()
            return self.total_dollars * weights[index_1based - 1]
        # equal
        return self.total_dollars / n


def signal_count_to_tranche_index(count: int, min_signals: int = 3) -> int:
    """3→1, 4→2, 5→3. <min returns 0 (no entry yet)."""
    if count < min_signals:
        return 0
    return count - min_signals + 1


def required_signals_for_tranche(index_1based: int, min_signals: int = 3) -> int:
    """Inverse: tranche 1 needs 3 signals, tranche 2 needs 4, tranche 3 needs 5."""
    return min_signals + index_1based - 1


def decide_next_action(
    state: ReversalState,
    bars: pd.DataFrame,
    today: dt.date,
) -> dict[str, Any]:
    """Pure planner: given current state and today's bars, return what action
    the caller should take. Does NOT mutate state — caller does that after
    confirming the action succeeded.

    Returns a dict with:
      - action: "place_tranche" | "place_protective_stop" | "abort_stalled" |
                "hold" | "complete"
      - signal_count, signals: latest reading
      - For "place_tranche": tranche_index (1-based), target_dollars
      - For "place_protective_stop": stop_price
      - For "abort_stalled": days_since_last_action
    """
    signals = count_signals(bars)
    count = sum(signals.values())
    cfg = state.config
    today_str = today.isoformat()

    decision: dict[str, Any] = {
        "signals": signals,
        "signal_count": count,
    }

    # Update consecutive-days counter (caller writes back)
    if state.last_check_date == today_str:
        # Same day re-check — counter doesn't move
        prospective_consec = state.consecutive_days_at_threshold
    elif count >= cfg.min_signals_for_entry and state.last_signal_count >= cfg.min_signals_for_entry:
        prospective_consec = state.consecutive_days_at_threshold + 1
    elif count >= cfg.min_signals_for_entry:
        prospective_consec = 1
    else:
        prospective_consec = 0
    decision["consecutive_days_at_threshold"] = prospective_consec

    # Terminal statuses do nothing
    if state.status in (
        ReversalStatus.COMPLETE,
        ReversalStatus.CANCELLED,
        ReversalStatus.LIQUIDATED,
        ReversalStatus.ABORTED,
    ):
        decision["action"] = "hold"
        return decision

    # Stall timeout check (only relevant once we're WATCHING with stale state
    # or PARTIALLY_FILLED waiting for the next tranche).
    if state.last_action_at:
        last_action = dt.date.fromisoformat(state.last_action_at)
        days_since = (today - last_action).days
        if days_since >= cfg.stall_timeout_days and state.status in (
            ReversalStatus.PARTIALLY_FILLED,
            ReversalStatus.STALLED,
        ):
            decision["action"] = "abort_stalled"
            decision["days_since_last_action"] = days_since
            return decision

    # Stop-and-wait: signals dropped after at least one tranche filled
    if (
        state.status == ReversalStatus.PARTIALLY_FILLED
        and count < cfg.min_signals_for_entry
        and not state.protective_stop_order_id
    ):
        last_fill = state.filled_tranches[-1]
        atr = _atr_today(bars)
        stop_price = round(last_fill.fill_price - cfg.protective_stop_atr_multiple * atr, 2)
        decision["action"] = "place_protective_stop"
        decision["stop_price"] = stop_price
        decision["atr"] = round(atr, 4)
        return decision

    # Already complete?
    if len(state.filled_tranches) >= cfg.tranche_count:
        decision["action"] = "complete"
        return decision

    # Should we place the next tranche?
    next_tranche_idx = len(state.filled_tranches) + 1
    required = required_signals_for_tranche(next_tranche_idx, cfg.min_signals_for_entry)
    if count >= required and prospective_consec >= cfg.signal_window_days:
        decision["action"] = "place_tranche"
        decision["tranche_index"] = next_tranche_idx
        decision["target_dollars"] = state.tranche_dollar_amount(next_tranche_idx)
        return decision

    decision["action"] = "hold"
    return decision


def _atr_today(bars: pd.DataFrame, length: int = 14) -> float:
    atr = ta.atr(bars["high"], bars["low"], bars["close"], length=length)
    return float(atr.iloc[-1])


# --- state persistence -----------------------------------------------------


def _state_to_dict(state: ReversalState) -> dict:
    d = asdict(state)
    d["status"] = state.status.value
    return d


def _state_from_dict(raw: dict) -> ReversalState:
    cfg = ReversalConfig(**raw.pop("config"))
    tranches = [FilledTranche(**t) for t in raw.pop("filled_tranches", [])]
    status = ReversalStatus(raw.pop("status"))
    return ReversalState(
        config=cfg, filled_tranches=tranches, status=status, **raw,
    )


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, ReversalState]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {sym: _state_from_dict(entry) for sym, entry in raw.items()}


def save_state(
    state: dict[str, ReversalState],
    path: Path = DEFAULT_STATE_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {sym: _state_to_dict(s) for sym, s in state.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True, default=str))


# --- info-only entry point ---------------------------------------------------


def check_reversal_signals_from_bars(
    symbol: str,
    bars: pd.DataFrame,
    min_signals_for_entry: int = 3,
) -> dict[str, Any]:
    """Pure function: report current signals + recommended tranche.

    Does NOT touch state — use this for the `check_reversal_signals` tool.
    The stateful tranching lives in `decide_next_action` + `ReversalState`.
    """
    signals = count_signals(bars)
    count = sum(signals.values())
    return {
        "symbol": symbol,
        "current_price": round(float(bars["close"].iloc[-1]), 2),
        "signal_count": count,
        "signals": signals,
        "recommended_tranche": signal_count_to_tranche_index(count, min_signals_for_entry),
    }
