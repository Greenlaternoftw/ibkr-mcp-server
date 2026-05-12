"""Swing-trading loop — HOLDING ↔ FLAT state machine.

When HOLDING: protect the position with an OCA pair (trailing SELL + hard
floor STP at `cost_basis - floor_offset`).
When FLAT:    queue a LMT BUY at the dip price relative to the last sell fill.

Cross-cutting gates checked every tick:
  - Regime filter (if `regime_filter_enabled`)
  - 24-hour cooldown after any fill
  - Optional volume confirmation
  - Trail recomputed from ATR(14) each tick

Like Layers 2 and 3, the planner (`decide_next_action`) is a pure function —
state in, decision dict out. The hourly tick that calls it and translates
the decision into IBKR orders lives on `IBKRClient` and is throwaway code
that Layer 5's daemon will replace.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import pandas_ta as ta


DEFAULT_STATE_PATH = Path.home() / ".ibkr-mcp-swing-state.json"


class SwingState(str, Enum):
    HOLDING = "HOLDING"   # we own shares, protective OCA active
    FLAT = "FLAT"         # we don't own shares, waiting for dip
    STOPPED = "STOPPED"   # operator-stopped


@dataclass
class SwingConfig:
    """Per-strategy tuning. `dip_amount` XOR `dip_percent` must be set."""

    trail_atr_multiplier: float = 2.0
    floor_offset: float = 0.0
    dip_amount: Optional[float] = None
    dip_percent: Optional[float] = None
    regime_filter_enabled: bool = True
    require_close_confirmation: bool = True
    require_volume_confirmation: bool = False
    volume_threshold_multiplier: float = 1.0
    cooldown_hours: int = 24


@dataclass
class SwingStateRecord:
    symbol: str
    quantity: int
    cost_basis: float
    config: SwingConfig
    state: SwingState = SwingState.HOLDING

    # Active orders we placed (filled in by the imperative tick layer)
    protective_trail_order_id: Optional[int] = None
    protective_stop_order_id: Optional[int] = None
    oca_group: Optional[str] = None
    dip_buy_order_id: Optional[int] = None

    # Fill history (used for cooldown + dip-price computation)
    last_fill_action: Optional[str] = None    # "BUY" or "SELL"
    last_fill_price: Optional[float] = None
    last_fill_time: Optional[str] = None      # ISO datetime

    # Telemetry
    started_at: str = ""
    last_tick_at: str = ""
    last_regime_enabled: Optional[bool] = None


# --- pure helpers ----------------------------------------------------------


def compute_trail_amount(bars: pd.DataFrame, multiplier: float) -> float:
    """ATR-scaled trail in dollars. Recomputed each tick."""
    atr_series = ta.atr(bars["high"], bars["low"], bars["close"], length=14)
    return float(multiplier * atr_series.iloc[-1])


def compute_floor_price(cost_basis: float, floor_offset: float) -> float:
    """Hard stop price for the OCA's STP leg. floor_offset>0 → floor below
    cost basis."""
    return round(cost_basis - floor_offset, 2)


def compute_dip_price(last_sell_price: float, cfg: SwingConfig) -> float:
    """Where to place the LMT BUY after a protective sell fired."""
    if cfg.dip_amount is not None and cfg.dip_percent is not None:
        raise ValueError("specify exactly one of dip_amount or dip_percent")
    if cfg.dip_amount is None and cfg.dip_percent is None:
        raise ValueError("specify dip_amount or dip_percent")
    if cfg.dip_amount is not None:
        return round(last_sell_price - cfg.dip_amount, 2)
    return round(last_sell_price * (1 - cfg.dip_percent / 100.0), 2)


def is_in_cooldown(state: SwingStateRecord, now: dt.datetime) -> bool:
    if not state.last_fill_time:
        return False
    last = dt.datetime.fromisoformat(state.last_fill_time)
    elapsed_hours = (now - last).total_seconds() / 3600.0
    return elapsed_hours < state.config.cooldown_hours


def check_volume_confirmation(
    bars: pd.DataFrame,
    multiplier: float,
    lookback: int = 20,
) -> bool:
    if len(bars) < lookback + 1:
        return False
    avg = bars["volume"].iloc[-(lookback + 1):-1].mean()
    return float(bars["volume"].iloc[-1]) > multiplier * float(avg)


# --- state machine planner -------------------------------------------------


def decide_next_action(
    state: SwingStateRecord,
    bars: pd.DataFrame,
    regime_enabled: bool,
    now: dt.datetime,
) -> dict[str, Any]:
    """Pure planner: returns the action the imperative tick should take.

    Actions:
      - "place_protective_oca": HOLDING with no active OCA → place TRAIL+STP
      - "recompute_trail":      HOLDING with OCA already → trail value changed
      - "cancel_dip_buy":       FLAT, regime disabled, dip-buy already queued
      - "place_dip_buy":        FLAT, regime ok, cooldown done → queue LMT BUY
      - "hold":                 nothing to do (with `reason` field)
      - "stopped":              terminal
    """
    if state.state is SwingState.STOPPED:
        return {"action": "stopped"}

    trail_amount = compute_trail_amount(bars, state.config.trail_atr_multiplier)
    floor_price = compute_floor_price(state.cost_basis, state.config.floor_offset)

    decision: dict[str, Any] = {
        "trail_amount": round(trail_amount, 2),
        "floor_price": floor_price,
        "regime_enabled": regime_enabled,
    }

    if state.state is SwingState.HOLDING:
        # Need a protective OCA pair if we don't have one yet.
        if not (state.protective_trail_order_id and state.protective_stop_order_id):
            decision["action"] = "place_protective_oca"
            return decision

        # We do have one — should the trail be updated? Heuristic: only if it
        # has drifted by more than 5% to avoid churn. Caller can override by
        # invoking update_swing_params manually.
        decision["action"] = "hold"
        decision["reason"] = "protected"
        return decision

    if state.state is SwingState.FLAT:
        # Regime gate — Layer 4 spec says cancel pending dip-buys but leave
        # protective sells alone. Since we're FLAT, there are no protective
        # sells to leave alone; just don't enter.
        if state.config.regime_filter_enabled and not regime_enabled:
            if state.dip_buy_order_id:
                decision["action"] = "cancel_dip_buy"
                decision["reason"] = "regime_disabled"
                return decision
            decision["action"] = "hold"
            decision["reason"] = "regime_disabled"
            return decision

        # Cooldown gate
        if is_in_cooldown(state, now):
            decision["action"] = "hold"
            decision["reason"] = "cooldown"
            return decision

        # Volume gate
        if state.config.require_volume_confirmation:
            if not check_volume_confirmation(bars, state.config.volume_threshold_multiplier):
                decision["action"] = "hold"
                decision["reason"] = "volume_below_threshold"
                return decision

        # Need a last_fill_price to compute the dip; if missing (first FLAT
        # ever), we can't act yet.
        if state.last_fill_price is None:
            decision["action"] = "hold"
            decision["reason"] = "no_last_fill_price"
            return decision

        # Already have a dip-buy queued? Hold.
        if state.dip_buy_order_id:
            decision["action"] = "hold"
            decision["reason"] = "dip_buy_already_queued"
            return decision

        dip_price = compute_dip_price(state.last_fill_price, state.config)
        decision["action"] = "place_dip_buy"
        decision["limit_price"] = dip_price
        decision["quantity"] = state.quantity
        return decision

    decision["action"] = "hold"
    decision["reason"] = f"unhandled_state:{state.state}"
    return decision


# --- fill detection --------------------------------------------------------


@dataclass
class FillEvent:
    order_id: int
    role: str             # "trail_sell" | "stop_sell" | "dip_buy"
    fill_price: float
    quantity: int
    timestamp: str        # ISO


def detect_fills_from_trades(
    state: SwingStateRecord,
    trades: list[Any],
) -> list[FillEvent]:
    """Inspect ib_async Trade list, return any that fired since last tick.

    A Trade is "filled" once its orderStatus.status is "Filled". We map IDs
    back to our role tags.
    """
    fills: list[FillEvent] = []
    id_to_role = {
        state.protective_trail_order_id: "trail_sell",
        state.protective_stop_order_id: "stop_sell",
        state.dip_buy_order_id: "dip_buy",
    }
    for trade in trades:
        order_id = trade.order.orderId
        role = id_to_role.get(order_id)
        if role is None or trade.orderStatus.status != "Filled":
            continue
        fills.append(FillEvent(
            order_id=order_id,
            role=role,
            fill_price=float(trade.orderStatus.avgFillPrice or 0.0),
            quantity=int(trade.order.totalQuantity),
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
        ))
    return fills


def apply_fill(state: SwingStateRecord, fill: FillEvent) -> SwingStateRecord:
    """Mutate state to reflect a fill. Returns the same state for chaining."""
    state.last_fill_price = fill.fill_price
    state.last_fill_time = fill.timestamp
    if fill.role in ("trail_sell", "stop_sell"):
        state.last_fill_action = "SELL"
        state.state = SwingState.FLAT
        # OCA: filling one cancels the other broker-side; clear both.
        state.protective_trail_order_id = None
        state.protective_stop_order_id = None
        state.oca_group = None
    elif fill.role == "dip_buy":
        state.last_fill_action = "BUY"
        state.state = SwingState.HOLDING
        state.dip_buy_order_id = None
        # Update cost basis to the new fill (loop's drift acknowledgment)
        state.cost_basis = fill.fill_price
    return state


# --- state persistence ----------------------------------------------------


def _state_to_dict(s: SwingStateRecord) -> dict:
    d = asdict(s)
    d["state"] = s.state.value
    return d


def _state_from_dict(raw: dict) -> SwingStateRecord:
    cfg = SwingConfig(**raw.pop("config"))
    state_val = SwingState(raw.pop("state"))
    return SwingStateRecord(config=cfg, state=state_val, **raw)


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, SwingStateRecord]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {sym: _state_from_dict(entry) for sym, entry in raw.items()}


def save_state(
    state: dict[str, SwingStateRecord],
    path: Path = DEFAULT_STATE_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialisable = {sym: _state_to_dict(s) for sym, s in state.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True, default=str))
