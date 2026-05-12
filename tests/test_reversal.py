"""Tests for the Layer 3 reversal entry — signal detection + tranche state machine.

All tests are pure (synthetic data + state objects); no IBKR connection needed.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ibkr_mcp_server.reversal import (
    FilledTranche,
    ReversalConfig,
    ReversalState,
    ReversalStatus,
    check_reversal_signals_from_bars,
    count_signals,
    decide_next_action,
    load_state,
    save_state,
    signal_count_to_tranche_index,
    signal_higher_low,
    signal_macd_crossover,
    signal_rsi_above_30,
    signal_rsi_divergence,
    signal_volume_surge,
    required_signals_for_tranche,
)


# --- synthetic-data helpers -------------------------------------------------


def _bars(opens, highs, lows, closes, volumes=None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="B"),
            "open": list(opens),
            "high": list(highs),
            "low": list(lows),
            "close": list(closes),
            "volume": list(volumes if volumes is not None else [1_000_000] * n),
        }
    )


def _walk(closes: np.ndarray) -> pd.DataFrame:
    """Build bars where high/low straddle close by a small fixed amount."""
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.3
    lows = np.minimum(opens, closes) - 0.3
    return _bars(opens, highs, lows, closes)


# --- Signal: RSI divergence -------------------------------------------------


class TestRsiDivergence:
    def test_classic_bullish_divergence(self):
        # First 60 bars: drop from 100 to 70 → RSI gets very low
        # Next 20 bars: recovery to 90
        # Last 20 bars: drop again to 65 (lower low) but slower → RSI is higher
        seg1 = np.linspace(100, 70, 60)
        seg2 = np.linspace(70, 90, 20)
        seg3 = np.linspace(90, 65, 20)  # lower low than seg1 (65 < 70)
        # But the descent is slower, so RSI at the new low should be higher
        # Actually slower descent ≠ higher RSI necessarily.
        # Build it differently: very flat last 20, with the low at the start of seg3.
        seg3 = np.array([72, 70, 68, 66, 65] + [66] * 7 + [68] * 8)
        closes = np.concatenate([seg1, seg2, seg3])
        bars = _walk(closes)
        assert signal_rsi_divergence(bars) is True

    def test_no_divergence_when_no_lower_low(self):
        # Recent 20 days low is higher than earlier 20 days low
        seg1 = np.linspace(100, 60, 60)
        seg2 = np.linspace(60, 75, 20)
        seg3 = np.linspace(75, 80, 20)
        closes = np.concatenate([seg1, seg2, seg3])
        bars = _walk(closes)
        assert signal_rsi_divergence(bars) is False

    def test_too_few_bars(self):
        bars = _walk(np.linspace(100, 90, 20))
        assert signal_rsi_divergence(bars) is False


# --- Signal: RSI above 30 ---------------------------------------------------


class TestRsiAbove30:
    def test_crosses_above(self):
        # Long downtrend → RSI well below 30, then sharp rebound → RSI > 30
        seg1 = np.linspace(100, 50, 50)  # steady decline
        seg2 = np.array([50, 55, 60, 65])  # sharp rebound
        bars = _walk(np.concatenate([seg1, seg2]))
        assert signal_rsi_above_30(bars) is True

    def test_no_cross_when_always_above(self):
        # Continuously rising → RSI never visits below 30
        closes = np.linspace(100, 130, 60)
        bars = _walk(closes)
        assert signal_rsi_above_30(bars) is False


# --- Signal: MACD bullish crossover ----------------------------------------


class TestMacdCrossover:
    def test_bullish_crossover(self):
        # Pattern empirically chosen to push the MACD/signal crossover into
        # the last 3 bars: 60 bars flat at 100, then a 30-bar decline, then
        # a sharp 3-bar recovery. MACD stays below signal through the decline
        # and only crosses up during the final recovery.
        flat = np.full(60, 100.0)
        decline = np.linspace(100, 50, 30)
        recovery = np.array([52.0, 60.0, 75.0])
        closes = np.concatenate([flat, decline, recovery])
        bars = _walk(closes)
        assert signal_macd_crossover(bars) is True

    def test_no_crossover_in_steady_uptrend(self):
        closes = np.linspace(100, 200, 100)
        bars = _walk(closes)
        assert signal_macd_crossover(bars) is False


# --- Signal: higher low pivots ---------------------------------------------


class TestHigherLow:
    def test_higher_low_confirmed(self):
        # Hand-crafted clean V pattern. First V bottoms at 70 (index 6),
        # second V bottoms at 75 (index 17) — strictly higher.
        closes = np.array([
            100, 95, 90, 85, 80, 75, 70, 75, 80, 85, 90,   # 0..10 first V at idx 6
            95, 100, 95, 90, 85, 80, 75, 80, 85, 90, 95,   # 11..21 second V at idx 17
            100, 105, 110, 115, 120, 125, 130, 135,        # 22..29 tail
        ], dtype=float)
        bars = _walk(closes)
        assert signal_higher_low(bars) is True

    def test_lower_low_fails(self):
        # First V at 75, second V at 65 — strictly LOWER, gate should fail.
        closes = np.array([
            100, 95, 90, 85, 80, 75, 80, 85, 90,           # first V at idx 5 (75)
            95, 100, 95, 90, 85, 80, 75, 70, 65, 70, 75,   # second V at idx 17 (65)
            80, 85, 90, 95, 100, 105, 110, 115,
        ], dtype=float)
        bars = _walk(closes)
        assert signal_higher_low(bars) is False


# --- Signal: volume surge --------------------------------------------------


class TestVolumeSurge:
    def test_surge_detected(self):
        closes = np.linspace(100, 110, 30)
        volumes = [1_000_000] * 28 + [800_000, 2_500_000]   # last bar: up-day with 2.5M vol
        bars = _bars(
            opens=[c for c in closes],
            highs=[c + 0.5 for c in closes],
            lows=[c - 0.5 for c in closes],
            closes=closes,
            volumes=volumes,
        )
        assert signal_volume_surge(bars) is True

    def test_no_surge_quiet_volume(self):
        closes = np.linspace(100, 110, 30)
        bars = _bars(
            opens=closes, highs=closes + 0.5, lows=closes - 0.5,
            closes=closes, volumes=[1_000_000] * 30,
        )
        assert signal_volume_surge(bars) is False


# --- count_signals + tranche-index mapping --------------------------------


class TestSignalCountAndTranche:
    def test_count_signals_returns_dict(self):
        closes = np.linspace(100, 110, 60)
        bars = _walk(closes)
        result = count_signals(bars)
        assert set(result.keys()) == {
            "rsi_divergence", "rsi_above_30", "macd_crossover",
            "higher_low", "volume_surge",
        }
        assert all(isinstance(v, bool) for v in result.values())

    def test_count_to_tranche_mapping(self):
        assert signal_count_to_tranche_index(0) == 0
        assert signal_count_to_tranche_index(2) == 0
        assert signal_count_to_tranche_index(3) == 1
        assert signal_count_to_tranche_index(4) == 2
        assert signal_count_to_tranche_index(5) == 3

    def test_required_signals_inverse(self):
        assert required_signals_for_tranche(1) == 3
        assert required_signals_for_tranche(2) == 4
        assert required_signals_for_tranche(3) == 5


# --- check_reversal_signals_from_bars -------------------------------------


class TestCheckReversalSignals:
    def test_returns_full_shape(self):
        closes = np.linspace(100, 110, 60)
        bars = _walk(closes)
        result = check_reversal_signals_from_bars("AAPL", bars)
        assert result["symbol"] == "AAPL"
        assert "signal_count" in result
        assert "signals" in result
        assert "recommended_tranche" in result
        assert "current_price" in result


# --- ReversalState helpers --------------------------------------------------


class TestTrancheSizing:
    def test_equal_sizing(self):
        s = ReversalState(symbol="AAPL", total_dollars=30000, config=ReversalConfig(tranche_count=3))
        assert s.tranche_dollar_amount(1) == 10000
        assert s.tranche_dollar_amount(2) == 10000
        assert s.tranche_dollar_amount(3) == 10000

    def test_weighted_sizing_three_tranches(self):
        s = ReversalState(symbol="AAPL", total_dollars=30000,
                          config=ReversalConfig(tranche_count=3, tranche_sizing="weighted"))
        assert s.tranche_dollar_amount(1) == 6000   # 20%
        assert s.tranche_dollar_amount(2) == 9000   # 30%
        assert s.tranche_dollar_amount(3) == 15000  # 50%


# --- decide_next_action ----------------------------------------------------


class _StubBars:
    """Minimal bars stub for tests that only need decide_next_action to walk
    its branches. Signal detectors will all see this as 'no signals'."""

    @staticmethod
    def flat(n: int = 120) -> pd.DataFrame:
        closes = np.full(n, 100.0)
        return _walk(closes)


def _force_signals(closes: np.ndarray, want_count: int) -> pd.DataFrame:
    """Helper for tests that need a known signal count. Returns synthetic bars
    where roughly `want_count` of the 5 signals fire. Used loosely — tests
    that need exact counts should fake bars and patch count_signals instead.
    """
    return _walk(closes)


class TestDecideNextAction:

    def _state(self, **overrides):
        cfg = ReversalConfig(**{k: v for k, v in overrides.items() if k in {
            "tranche_count", "tranche_sizing", "min_signals_for_entry",
            "signals_per_tranche", "signal_window_days", "stall_timeout_days",
            "protective_stop_atr_multiple",
        }})
        return ReversalState(
            symbol="AAPL", total_dollars=30000, config=cfg,
            started_at="2024-01-01", last_action_at="2024-01-01",
            **{k: v for k, v in overrides.items() if k in {
                "status", "last_signal_count", "consecutive_days_at_threshold",
                "last_check_date", "filled_tranches", "protective_stop_order_id",
            }},
        )

    def test_terminal_status_holds(self, monkeypatch):
        state = self._state(status=ReversalStatus.COMPLETE)
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {k: False for k in ["rsi_divergence", "rsi_above_30", "macd_crossover", "higher_low", "volume_surge"]},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 10))
        assert d["action"] == "hold"

    def test_holds_when_below_threshold(self, monkeypatch):
        state = self._state()
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": False, "rsi_above_30": False, "macd_crossover": False, "higher_low": True, "volume_surge": True},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 10))
        assert d["action"] == "hold"
        assert d["signal_count"] == 2

    def test_places_tranche_when_threshold_met_with_smoothing(self, monkeypatch):
        state = self._state(
            last_signal_count=3,
            consecutive_days_at_threshold=2,
            last_check_date="2024-01-09",
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": True, "rsi_above_30": True, "macd_crossover": True, "higher_low": False, "volume_surge": False},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 10))
        assert d["action"] == "place_tranche"
        assert d["tranche_index"] == 1
        assert d["target_dollars"] == 10000.0  # 30000 / 3 equal
        assert d["consecutive_days_at_threshold"] == 3

    def test_holds_when_smoothing_not_satisfied(self, monkeypatch):
        state = self._state(
            last_signal_count=3,
            consecutive_days_at_threshold=1,
            last_check_date="2024-01-09",
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": True, "rsi_above_30": True, "macd_crossover": True, "higher_low": False, "volume_surge": False},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 10))
        assert d["action"] == "hold"

    def test_stop_and_wait_after_tranche_1(self, monkeypatch):
        state = self._state(
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[FilledTranche(
                index=1, target_dollars=10000, shares=100, fill_price=100.0, filled_at="2024-01-05",
            )],
            last_action_at="2024-01-05",
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": False, "rsi_above_30": False, "macd_crossover": False, "higher_low": False, "volume_surge": False},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 7))
        assert d["action"] == "place_protective_stop"
        assert d["stop_price"] < 100.0
        assert d["atr"] > 0

    def test_no_protective_stop_if_one_already_exists(self, monkeypatch):
        state = self._state(
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[FilledTranche(
                index=1, target_dollars=10000, shares=100, fill_price=100.0, filled_at="2024-01-05",
            )],
            protective_stop_order_id=42,
            last_action_at="2024-01-05",
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": False, "rsi_above_30": False, "macd_crossover": False, "higher_low": False, "volume_surge": False},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 7))
        assert d["action"] == "hold"

    def test_stall_timeout_aborts(self, monkeypatch):
        state = self._state(
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[FilledTranche(
                index=1, target_dollars=10000, shares=100, fill_price=100.0, filled_at="2024-01-01",
            )],
            last_action_at="2024-01-01",
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": True, "rsi_above_30": True, "macd_crossover": True, "higher_low": False, "volume_surge": False},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 15))  # 14 days later
        assert d["action"] == "abort_stalled"
        assert d["days_since_last_action"] == 14

    def test_completes_when_all_tranches_filled(self, monkeypatch):
        state = self._state(
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[
                FilledTranche(index=i, target_dollars=10000, shares=100, fill_price=100.0, filled_at="2024-01-05")
                for i in (1, 2, 3)
            ],
        )
        monkeypatch.setattr(
            "ibkr_mcp_server.reversal.count_signals",
            lambda _bars: {"rsi_divergence": True, "rsi_above_30": True, "macd_crossover": True, "higher_low": True, "volume_surge": True},
        )
        d = decide_next_action(state, _StubBars.flat(), dt.date(2024, 1, 10))
        assert d["action"] == "complete"


# --- state persistence ----------------------------------------------------


class TestReversalStatePersistence:
    def test_save_and_load_round_trip(self, tmp_path: Path):
        path = tmp_path / "rev.json"
        states = {
            "AAPL": ReversalState(
                symbol="AAPL", total_dollars=30000,
                config=ReversalConfig(tranche_count=3),
                status=ReversalStatus.PARTIALLY_FILLED,
                last_signal_count=4,
                filled_tranches=[
                    FilledTranche(index=1, target_dollars=10000, shares=100, fill_price=200.0, filled_at="2024-01-05"),
                ],
                started_at="2024-01-01",
                last_action_at="2024-01-05",
            )
        }
        save_state(states, path)
        loaded = load_state(path)
        assert loaded["AAPL"].status is ReversalStatus.PARTIALLY_FILLED
        assert loaded["AAPL"].last_signal_count == 4
        assert len(loaded["AAPL"].filled_tranches) == 1
        assert loaded["AAPL"].filled_tranches[0].shares == 100

    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert load_state(tmp_path / "nope.json") == {}
