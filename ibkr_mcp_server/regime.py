"""Regime filter — determine whether market conditions favor dip-buying.

Three gates, evaluated against OHLCV bars (daily, most recent last):

1. Trend rising:     SMA(50) today > SMA(50) five days ago
2. Trend strength:   ADX(14) below threshold (default 25)
3. Volatility calm:  ATR%(14) today < average ATR%(14) over last 100 days

Anti-whipsaw: a per-symbol consecutive-days counter is persisted to a state
file. Callers should only act on `sticky_enabled` (which goes True after
`smoothing_days` consecutive agreeing readings), not on the bare `enabled`
flag which can flip day-to-day.

Layer 5's daemon will replace the JSON state file with the SQLite store; the
read/write helpers below are intentionally simple so that swap touches only
this module.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pandas_ta as ta


DEFAULT_STATE_PATH = Path.home() / ".ibkr-mcp-regime-state.json"


@dataclass
class RegimeConfig:
    adx_threshold: float = 25.0
    atr_lookback: int = 100
    sma_period: int = 50
    sma_lookback_days: int = 5
    require_all_gates: bool = True
    # Number of consecutive same-direction readings before the regime sticks.
    smoothing_days: int = 3


@dataclass
class RegimeStateEntry:
    """Per-symbol consecutive-days tracking."""

    last_enabled: bool | None = None
    consecutive_days: int = 0
    last_check: str = ""

    def update(self, enabled: bool, today_str: str) -> None:
        if self.last_check == today_str:
            # Same calendar day → don't double-count, just refresh the value.
            self.last_enabled = enabled
            return
        if enabled == self.last_enabled:
            self.consecutive_days += 1
        else:
            self.consecutive_days = 1
        self.last_enabled = enabled
        self.last_check = today_str


def evaluate_gates(bars: pd.DataFrame, config: RegimeConfig) -> dict[str, Any]:
    """Compute the three gates. Bars are OHLCV with most recent last."""
    min_bars = max(
        config.sma_period + config.sma_lookback_days + 1,
        config.atr_lookback + 15,  # ATR(14) needs 14 + 1, then 100 more for the rolling avg
        28,  # ADX warmup
    )
    if len(bars) < min_bars:
        raise ValueError(
            f"Need at least {min_bars} bars to compute regime; got {len(bars)}"
        )

    close = bars["close"]
    high = bars["high"]
    low = bars["low"]

    # --- Gate 1: trend rising ------------------------------------------------
    sma = ta.sma(close, length=config.sma_period)
    sma_today = float(sma.iloc[-1])
    sma_n_ago = float(sma.iloc[-1 - config.sma_lookback_days])
    trend_rising = sma_today > sma_n_ago

    # --- Gate 2: ADX below threshold -----------------------------------------
    adx_df = ta.adx(high, low, close, length=14)
    # pandas_ta returns a DataFrame with columns ADX_14, DMP_14, DMN_14.
    adx_col = [c for c in adx_df.columns if c.startswith("ADX_")][0]
    adx_today = float(adx_df[adx_col].iloc[-1])
    trend_strength_ok = adx_today < config.adx_threshold

    # --- Gate 3: ATR% below its rolling average ------------------------------
    atr = ta.atr(high, low, close, length=14)
    atr_pct = (atr / close) * 100.0
    atr_pct_today = float(atr_pct.iloc[-1])
    atr_pct_avg = float(atr_pct.iloc[-config.atr_lookback :].mean())
    volatility_calm = atr_pct_today < atr_pct_avg

    return {
        "trend_rising": {
            "pass": bool(trend_rising),
            f"sma{config.sma_period}_today": round(sma_today, 2),
            f"sma{config.sma_period}_{config.sma_lookback_days}_ago": round(sma_n_ago, 2),
        },
        "trend_strength_ok": {
            "pass": bool(trend_strength_ok),
            "adx": round(adx_today, 2),
            "threshold": config.adx_threshold,
        },
        "volatility_calm": {
            "pass": bool(volatility_calm),
            "atr_pct": round(atr_pct_today, 4),
            "atr_pct_avg": round(atr_pct_avg, 4),
        },
    }


def aggregate_enabled(gates: dict, require_all: bool) -> bool:
    passes = [g["pass"] for g in gates.values()]
    return all(passes) if require_all else sum(passes) >= 2


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, RegimeStateEntry]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {symbol: RegimeStateEntry(**entry) for symbol, entry in raw.items()}


def save_state(
    state: dict[str, RegimeStateEntry],
    path: Path = DEFAULT_STATE_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {symbol: asdict(entry) for symbol, entry in state.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True))


def check_regime_from_bars(
    symbol: str,
    bars: pd.DataFrame,
    config: RegimeConfig | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Pure function: given OHLCV and config, return the full regime dict.

    All I/O (state persistence) is contained here so tests can pass a temp
    state path. The IBKR-data-fetching wrapper lives on `IBKRClient`.
    """
    config = config or RegimeConfig()
    today = today or dt.date.today()
    today_str = today.isoformat()

    gates = evaluate_gates(bars, config)
    enabled = aggregate_enabled(gates, config.require_all_gates)

    state = load_state(state_path)
    entry = state.get(symbol, RegimeStateEntry())
    entry.update(enabled, today_str)
    state[symbol] = entry
    save_state(state, state_path)

    return {
        "enabled": bool(enabled),
        "symbol": symbol,
        "price": round(float(bars["close"].iloc[-1]), 2),
        "gates": gates,
        "consecutive_days_enabled": entry.consecutive_days,
        "smoothing_required_days": config.smoothing_days,
        "sticky_enabled": entry.consecutive_days >= config.smoothing_days and bool(enabled),
        "sticky_disabled": entry.consecutive_days >= config.smoothing_days and not bool(enabled),
    }
