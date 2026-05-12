"""Tests for the Layer 4 swing-trading loop — state machine + helpers.

All tests are pure (no IBKR connection). The imperative tick on `IBKRClient`
is tested separately via mocked `ib` in the existing test_orders fixtures.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ibkr_mcp_server.swing import (
    FillEvent,
    SwingConfig,
    SwingState,
    SwingStateRecord,
    apply_fill,
    check_volume_confirmation,
    compute_dip_price,
    compute_floor_price,
    compute_trail_amount,
    decide_next_action,
    detect_fills_from_trades,
    is_in_cooldown,
    load_state,
    save_state,
)


# --- bar helpers -----------------------------------------------------------


def _bars(n: int = 60, drift: float = 0.0, vol_last: float = 1_000_000) -> pd.DataFrame:
    """Simple synthetic bars: closes drift gently, fixed +/-0.3 range."""
    closes = 100 + np.cumsum(np.full(n, drift))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="B"),
        "open": opens,
        "high": np.maximum(opens, closes) + 0.3,
        "low": np.minimum(opens, closes) - 0.3,
        "close": closes,
        "volume": [1_000_000] * (n - 1) + [vol_last],
    })


def _state(**overrides) -> SwingStateRecord:
    cfg_fields = {
        "trail_atr_multiplier", "floor_offset", "dip_amount", "dip_percent",
        "regime_filter_enabled", "require_close_confirmation",
        "require_volume_confirmation", "volume_threshold_multiplier", "cooldown_hours",
    }
    cfg = SwingConfig(**{k: v for k, v in overrides.items() if k in cfg_fields})
    # Default to dip_percent=3.0 unless caller specified one
    if cfg.dip_amount is None and cfg.dip_percent is None:
        cfg.dip_percent = 3.0
    rec_fields = {
        "symbol", "quantity", "cost_basis", "state",
        "protective_trail_order_id", "protective_stop_order_id", "oca_group",
        "dip_buy_order_id", "last_fill_action", "last_fill_price", "last_fill_time",
        "started_at", "last_tick_at", "last_regime_enabled",
    }
    rec_kwargs = {k: v for k, v in overrides.items() if k in rec_fields}
    rec_kwargs.setdefault("symbol", "AAPL")
    rec_kwargs.setdefault("quantity", 100)
    rec_kwargs.setdefault("cost_basis", 260.0)
    return SwingStateRecord(config=cfg, **rec_kwargs)


# --- pure helpers ----------------------------------------------------------


class TestPureHelpers:
    def test_compute_trail_amount(self):
        bars = _bars()
        trail = compute_trail_amount(bars, multiplier=2.0)
        assert trail > 0
        # Doubling the multiplier should double the trail
        assert compute_trail_amount(bars, multiplier=4.0) == pytest.approx(trail * 2, rel=1e-9)

    def test_compute_floor_price(self):
        assert compute_floor_price(260.0, floor_offset=5.0) == 255.0
        assert compute_floor_price(260.0, floor_offset=0.0) == 260.0

    def test_compute_dip_price_amount(self):
        cfg = SwingConfig(dip_amount=3.0)
        assert compute_dip_price(260.0, cfg) == 257.0

    def test_compute_dip_price_percent(self):
        cfg = SwingConfig(dip_percent=3.0)
        # 260 * 0.97 = 252.2
        assert compute_dip_price(260.0, cfg) == pytest.approx(252.2, abs=0.01)

    def test_dip_requires_exactly_one(self):
        with pytest.raises(ValueError):
            compute_dip_price(260.0, SwingConfig())
        with pytest.raises(ValueError):
            compute_dip_price(260.0, SwingConfig(dip_amount=3.0, dip_percent=3.0))

    def test_is_in_cooldown(self):
        state = _state(
            last_fill_time=dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone.utc).isoformat(),
            cooldown_hours=24,
        )
        # 12 hours later → still in cooldown
        assert is_in_cooldown(state, dt.datetime(2024, 1, 2, 0, 0, tzinfo=dt.timezone.utc)) is True
        # 25 hours later → out of cooldown
        assert is_in_cooldown(state, dt.datetime(2024, 1, 2, 13, 0, tzinfo=dt.timezone.utc)) is False

    def test_cooldown_with_no_fill_history(self):
        state = _state()
        assert is_in_cooldown(state, dt.datetime.now(dt.timezone.utc)) is False

    def test_volume_confirmation(self):
        # Last bar volume 3M, average of prior 20 is 1M → 3x → confirms
        bars = _bars(vol_last=3_000_000)
        assert check_volume_confirmation(bars, multiplier=1.0) is True
        assert check_volume_confirmation(bars, multiplier=2.5) is True
        assert check_volume_confirmation(bars, multiplier=4.0) is False

    def test_volume_confirmation_quiet(self):
        bars = _bars(vol_last=500_000)
        assert check_volume_confirmation(bars, multiplier=1.0) is False


# --- decide_next_action ----------------------------------------------------


class TestDecideNextAction:

    NOW = dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    def test_stopped_returns_stopped(self):
        state = _state(state=SwingState.STOPPED)
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "stopped"

    def test_holding_no_oca_places_protective(self):
        state = _state(state=SwingState.HOLDING)
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "place_protective_oca"
        assert d["trail_amount"] > 0
        assert d["floor_price"] == 260.0

    def test_holding_with_oca_holds(self):
        state = _state(
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
        )
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "protected"

    def test_flat_regime_disabled_cancels_pending_dip_buy(self):
        state = _state(
            state=SwingState.FLAT,
            last_fill_price=255.0, dip_buy_order_id=42,
        )
        d = decide_next_action(state, _bars(), regime_enabled=False, now=self.NOW)
        assert d["action"] == "cancel_dip_buy"
        assert d["reason"] == "regime_disabled"

    def test_flat_regime_disabled_no_pending_holds(self):
        state = _state(state=SwingState.FLAT, last_fill_price=255.0)
        d = decide_next_action(state, _bars(), regime_enabled=False, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "regime_disabled"

    def test_flat_in_cooldown_holds(self):
        state = _state(
            state=SwingState.FLAT,
            last_fill_price=255.0,
            last_fill_time=(self.NOW - dt.timedelta(hours=1)).isoformat(),
            cooldown_hours=24,
        )
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "cooldown"

    def test_flat_volume_gate_blocks_when_required(self):
        state = _state(
            state=SwingState.FLAT,
            last_fill_price=255.0,
            require_volume_confirmation=True,
            volume_threshold_multiplier=2.0,
        )
        # Default _bars has vol_last == 1M == avg, so 1.0x not >2.0x → blocked
        d = decide_next_action(state, _bars(vol_last=1_000_000),
                                regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "volume_below_threshold"

    def test_flat_volume_gate_passes_with_high_volume(self):
        state = _state(
            state=SwingState.FLAT,
            last_fill_price=255.0,
            require_volume_confirmation=True,
            volume_threshold_multiplier=1.5,
        )
        d = decide_next_action(state, _bars(vol_last=3_000_000),
                                regime_enabled=True, now=self.NOW)
        assert d["action"] == "place_dip_buy"

    def test_flat_no_last_fill_holds(self):
        state = _state(state=SwingState.FLAT, last_fill_price=None)
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "no_last_fill_price"

    def test_flat_dip_buy_already_queued_holds(self):
        state = _state(
            state=SwingState.FLAT,
            last_fill_price=255.0,
            dip_buy_order_id=42,
        )
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "dip_buy_already_queued"

    def test_flat_all_gates_pass_places_dip_buy(self):
        state = _state(state=SwingState.FLAT, last_fill_price=260.0, dip_percent=3.0)
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "place_dip_buy"
        assert d["limit_price"] == pytest.approx(252.2, abs=0.01)
        assert d["quantity"] == 100


# --- fill detection + state transitions -----------------------------------


class _FakeOrder:
    def __init__(self, order_id: int, qty: int):
        self.orderId = order_id
        self.totalQuantity = qty


class _FakeStatus:
    def __init__(self, status: str, avg: float):
        self.status = status
        self.avgFillPrice = avg


class _FakeTrade:
    def __init__(self, order_id: int, qty: int, status: str, avg: float):
        self.order = _FakeOrder(order_id, qty)
        self.orderStatus = _FakeStatus(status, avg)


class TestFillDetection:
    def test_detects_trail_fill(self):
        state = _state(
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
        )
        trades = [
            _FakeTrade(10, 100, "Filled", avg=258.5),
            _FakeTrade(11, 100, "Cancelled", avg=0.0),  # OCA partner cancelled
        ]
        fills = detect_fills_from_trades(state, trades)
        assert len(fills) == 1
        assert fills[0].role == "trail_sell"
        assert fills[0].fill_price == 258.5

    def test_detects_dip_buy_fill(self):
        state = _state(state=SwingState.FLAT, dip_buy_order_id=42, last_fill_price=260.0)
        trades = [_FakeTrade(42, 100, "Filled", avg=252.20)]
        fills = detect_fills_from_trades(state, trades)
        assert len(fills) == 1
        assert fills[0].role == "dip_buy"

    def test_ignores_unrelated_trades(self):
        state = _state(
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
        )
        trades = [_FakeTrade(999, 100, "Filled", avg=100.0)]
        assert detect_fills_from_trades(state, trades) == []

    def test_ignores_unfilled_trades(self):
        state = _state(state=SwingState.HOLDING, protective_trail_order_id=10)
        trades = [_FakeTrade(10, 100, "Submitted", avg=0.0)]
        assert detect_fills_from_trades(state, trades) == []


class TestApplyFill:
    def test_trail_fill_transitions_to_flat(self):
        state = _state(
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
            oca_group="grp-1",
        )
        fill = FillEvent(order_id=10, role="trail_sell", fill_price=258.5,
                         quantity=100, timestamp="2024-06-01T12:00:00+00:00")
        apply_fill(state, fill)
        assert state.state is SwingState.FLAT
        assert state.last_fill_action == "SELL"
        assert state.last_fill_price == 258.5
        assert state.protective_trail_order_id is None
        assert state.protective_stop_order_id is None
        assert state.oca_group is None

    def test_dip_buy_fill_transitions_to_holding(self):
        state = _state(
            state=SwingState.FLAT,
            dip_buy_order_id=42,
            last_fill_price=260.0,
        )
        fill = FillEvent(order_id=42, role="dip_buy", fill_price=252.20,
                         quantity=100, timestamp="2024-06-02T15:00:00+00:00")
        apply_fill(state, fill)
        assert state.state is SwingState.HOLDING
        assert state.last_fill_action == "BUY"
        assert state.last_fill_price == 252.20
        assert state.cost_basis == 252.20  # drift acknowledgment
        assert state.dip_buy_order_id is None


# --- full HOLDING ↔ FLAT cycle ---------------------------------------------


class TestFullCycle:
    """Drive the state machine through a complete HOLDING → FLAT → HOLDING loop
    using mocked fills, asserting `decide_next_action` produces the right action
    at every step."""

    NOW = dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    def test_full_cycle(self):
        state = _state(
            state=SwingState.HOLDING,
            quantity=100, cost_basis=260.0,
            dip_percent=3.0,
            regime_filter_enabled=False,  # disable to focus on cycle
        )

        # Step 1: HOLDING with no OCA → should place protective
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "place_protective_oca"

        # Simulate placement
        state.protective_trail_order_id = 10
        state.protective_stop_order_id = 11
        state.oca_group = "grp-1"

        # Step 2: HOLDING with OCA → hold
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"

        # Step 3: trailing sell fires
        fill = FillEvent(order_id=10, role="trail_sell", fill_price=258.0,
                         quantity=100, timestamp=self.NOW.isoformat())
        apply_fill(state, fill)
        assert state.state is SwingState.FLAT

        # Step 4: FLAT, in cooldown immediately after fill → hold
        d = decide_next_action(state, _bars(), regime_enabled=True, now=self.NOW)
        assert d["action"] == "hold"
        assert d["reason"] == "cooldown"

        # Step 5: 25 hours later → cooldown over → place dip buy
        later = self.NOW + dt.timedelta(hours=25)
        d = decide_next_action(state, _bars(), regime_enabled=True, now=later)
        assert d["action"] == "place_dip_buy"
        assert d["limit_price"] == pytest.approx(258.0 * 0.97, abs=0.01)

        # Simulate placement
        state.dip_buy_order_id = 42

        # Step 6: FLAT with dip-buy queued → hold
        d = decide_next_action(state, _bars(), regime_enabled=True, now=later)
        assert d["action"] == "hold"
        assert d["reason"] == "dip_buy_already_queued"

        # Step 7: dip buy fills
        fill = FillEvent(order_id=42, role="dip_buy",
                         fill_price=258.0 * 0.97,
                         quantity=100, timestamp=later.isoformat())
        apply_fill(state, fill)
        assert state.state is SwingState.HOLDING
        assert state.cost_basis == pytest.approx(258.0 * 0.97, abs=0.01)

        # Step 8: back to HOLDING with no protective OCA → place new one
        d = decide_next_action(state, _bars(), regime_enabled=True, now=later)
        assert d["action"] == "place_protective_oca"


# --- state persistence ----------------------------------------------------


class TestSwingStatePersistence:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "swing.json"
        states = {
            "AAPL": _state(
                state=SwingState.HOLDING, quantity=100, cost_basis=260.0,
                dip_percent=3.0, last_fill_price=260.0,
                protective_trail_order_id=10, protective_stop_order_id=11,
                oca_group="grp-1",
            ),
        }
        save_state(states, path)
        loaded = load_state(path)
        assert loaded["AAPL"].state is SwingState.HOLDING
        assert loaded["AAPL"].quantity == 100
        assert loaded["AAPL"].protective_trail_order_id == 10
        assert loaded["AAPL"].oca_group == "grp-1"

    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert load_state(tmp_path / "nope.json") == {}
