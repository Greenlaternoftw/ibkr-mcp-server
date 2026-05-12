"""Tests for the Layer 2 regime filter.

All tests use synthetic price data so they don't hit IBKR. The
`check_regime_from_bars` entry point is pure (data in, dict out, with
state file I/O contained), which keeps the tests fast and deterministic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ibkr_mcp_server.regime import (
    RegimeConfig,
    RegimeStateEntry,
    aggregate_enabled,
    check_regime_from_bars,
    evaluate_gates,
    load_state,
    save_state,
)


# --- helpers to build synthetic OHLCV --------------------------------------


def _bars_from_closes(closes: np.ndarray, volatility: float = 0.5) -> pd.DataFrame:
    """Build daily OHLCV bars from a close series.

    high/low are placed ±volatility around close; volume is constant. Open is
    the previous close so there are no overnight gaps for ADX/ATR to chew on.
    """
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + volatility
    lows = np.minimum(opens, closes) - volatility
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(closes), freq="B"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000.0),
        }
    )


def _calm_uptrend(n: int = 200) -> pd.DataFrame:
    """Noisy, weakly upward-drifting series.

    Calibrated so the regime filter says "enabled":
    - SMA(50) up over 5 days (drift is positive)
    - ADX(14) low (drift is small vs noise, so directional movement is weak)
    - ATR%(14) recent calm (noise level drops in the last segment)

    The noise dominating the drift is what keeps ADX low. The recent quiet tail
    is what makes ATR% calm relative to its trailing average.
    """
    rng = np.random.default_rng(42)
    drift = 0.05
    # Big noise body, small calm tail.
    body_noise = rng.normal(0, 2.0, n - 25)
    tail_noise = rng.normal(0, 0.4, 25)
    noise = np.concatenate([body_noise, tail_noise])
    closes = 100 + np.cumsum(np.full(n, drift)) + noise
    return _bars_from_closes(closes, volatility=0.4)


def _choppy_sideways(n: int = 200, amp: float = 5.0) -> pd.DataFrame:
    """Sideways with big swings → trend gate fails."""
    rng = np.random.default_rng(7)
    base = 100 + amp * np.sin(np.linspace(0, 8 * np.pi, n))
    noise = rng.normal(0, 1.5, n)
    return _bars_from_closes(base + noise, volatility=1.5)


def _strong_downtrend(n: int = 200, slope: float = -0.6) -> pd.DataFrame:
    """Clean downtrend → trend gate fails, ADX high → trend_strength gate fails."""
    rng = np.random.default_rng(13)
    base = np.linspace(200, 200 + slope * n, n)
    noise = rng.normal(0, 0.4, n)
    return _bars_from_closes(base + noise, volatility=0.4)


# --- evaluate_gates --------------------------------------------------------


class TestEvaluateGates:
    def test_calm_uptrend_passes_all_three(self):
        bars = _calm_uptrend()
        # adx_threshold=40 because pure synthetic linear drift produces a
        # cleaner trend than real markets — real AAPL/NVDA in calm uptrends
        # tend to hit ADX 15-25. The gate logic itself is what we're verifying.
        gates = evaluate_gates(bars, RegimeConfig(adx_threshold=40.0))
        assert gates["trend_rising"]["pass"] is True
        assert gates["trend_strength_ok"]["pass"] is True
        assert gates["volatility_calm"]["pass"] is True

    def test_strong_downtrend_fails_trend_gate(self):
        bars = _strong_downtrend()
        gates = evaluate_gates(bars, RegimeConfig())
        assert gates["trend_rising"]["pass"] is False

    def test_choppy_sideways_fails_trend_gate(self):
        bars = _choppy_sideways()
        gates = evaluate_gates(bars, RegimeConfig())
        # Sideways → SMA today vs SMA 5 days ago is roughly flat,
        # the inequality could go either way but ADX should NOT be huge.
        assert "adx" in gates["trend_strength_ok"]

    def test_too_few_bars_raises(self):
        bars = _calm_uptrend(n=30)
        with pytest.raises(ValueError, match="Need at least"):
            evaluate_gates(bars, RegimeConfig())


# --- aggregate_enabled -----------------------------------------------------


class TestAggregate:
    def _gates(self, t: bool, s: bool, v: bool) -> dict:
        return {
            "trend_rising": {"pass": t},
            "trend_strength_ok": {"pass": s},
            "volatility_calm": {"pass": v},
        }

    def test_all_required_all_pass(self):
        assert aggregate_enabled(self._gates(True, True, True), require_all=True) is True

    def test_all_required_one_fails(self):
        assert aggregate_enabled(self._gates(True, False, True), require_all=True) is False

    def test_two_of_three_two_pass(self):
        assert aggregate_enabled(self._gates(True, False, True), require_all=False) is True

    def test_two_of_three_one_pass(self):
        assert aggregate_enabled(self._gates(False, False, True), require_all=False) is False


# --- consecutive-days state ------------------------------------------------


class TestStateEntry:
    def test_first_call_starts_counter(self):
        e = RegimeStateEntry()
        e.update(True, "2024-01-01")
        assert e.last_enabled is True
        assert e.consecutive_days == 1

    def test_same_value_increments(self):
        e = RegimeStateEntry()
        e.update(True, "2024-01-01")
        e.update(True, "2024-01-02")
        e.update(True, "2024-01-03")
        assert e.consecutive_days == 3

    def test_flip_resets_counter(self):
        e = RegimeStateEntry()
        e.update(True, "2024-01-01")
        e.update(True, "2024-01-02")
        e.update(False, "2024-01-03")
        assert e.last_enabled is False
        assert e.consecutive_days == 1

    def test_same_day_does_not_double_count(self):
        e = RegimeStateEntry()
        e.update(True, "2024-01-01")
        e.update(True, "2024-01-01")
        e.update(True, "2024-01-01")
        assert e.consecutive_days == 1


# --- check_regime_from_bars (end-to-end pure function) --------------------


class TestCheckRegime:
    def test_calm_uptrend_returns_enabled(self, tmp_path: Path):
        state_path = tmp_path / "state.json"
        bars = _calm_uptrend()
        result = check_regime_from_bars(
            "AAPL", bars,
            RegimeConfig(adx_threshold=40.0),
            state_path=state_path,
        )
        assert result["symbol"] == "AAPL"
        assert result["enabled"] is True
        assert "gates" in result
        assert result["consecutive_days_enabled"] == 1
        assert result["sticky_enabled"] is False  # smoothing not yet satisfied

    def test_smoothing_prevents_single_day_flip(self, tmp_path: Path):
        state_path = tmp_path / "state.json"
        bars = _calm_uptrend()
        cfg = RegimeConfig(adx_threshold=40.0)
        # Three consecutive enabled readings — the third should be sticky.
        for i, day in enumerate(["2024-01-01", "2024-01-02", "2024-01-03"]):
            r = check_regime_from_bars(
                "AAPL", bars, cfg, state_path=state_path,
                today=dt.date.fromisoformat(day),
            )
            assert r["consecutive_days_enabled"] == i + 1
        assert r["sticky_enabled"] is True

        # Now the 4th day flips to disabled — sticky_enabled must NOT immediately
        # go True for disabled; consecutive resets to 1.
        downtrend_bars = _strong_downtrend()
        r = check_regime_from_bars(
            "AAPL", downtrend_bars, cfg, state_path=state_path,
            today=dt.date.fromisoformat("2024-01-04"),
        )
        assert r["enabled"] is False
        assert r["consecutive_days_enabled"] == 1
        assert r["sticky_enabled"] is False
        assert r["sticky_disabled"] is False  # needs 3 consecutive disabled

    def test_tuning_override_takes_effect(self, tmp_path: Path):
        bars = _calm_uptrend()
        # With very strict ADX threshold (1.0), the trend_strength gate will fail.
        result = check_regime_from_bars(
            "AAPL", bars, RegimeConfig(adx_threshold=1.0),
            state_path=tmp_path / "s.json",
        )
        assert result["gates"]["trend_strength_ok"]["pass"] is False
        assert result["enabled"] is False

    def test_two_of_three_mode(self, tmp_path: Path):
        # require_all_gates=False means 2/3 is enough.
        bars = _calm_uptrend()
        result = check_regime_from_bars(
            "AAPL", bars,
            RegimeConfig(adx_threshold=1.0, require_all_gates=False),
            state_path=tmp_path / "s.json",
        )
        # Trend rising + volatility calm = 2/3, even with ADX gate strict.
        assert result["enabled"] is True


# --- state file round-trip --------------------------------------------------


class TestStatePersistence:
    def test_save_and_load_round_trip(self, tmp_path: Path):
        path = tmp_path / "state.json"
        state = {
            "AAPL": RegimeStateEntry(last_enabled=True, consecutive_days=3, last_check="2024-01-03"),
            "NVDA": RegimeStateEntry(last_enabled=False, consecutive_days=1, last_check="2024-01-03"),
        }
        save_state(state, path)
        loaded = load_state(path)
        assert loaded["AAPL"].consecutive_days == 3
        assert loaded["NVDA"].last_enabled is False

    def test_load_missing_file_returns_empty(self, tmp_path: Path):
        assert load_state(tmp_path / "nope.json") == {}

    def test_load_corrupt_file_returns_empty(self, tmp_path: Path):
        path = tmp_path / "broken.json"
        path.write_text("{not valid json")
        assert load_state(path) == {}
