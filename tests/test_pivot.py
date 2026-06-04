"""Tests for the pivot-loop analysis module.

Pure logic -- no IBKR, no network, no yfinance. Builds synthetic bars
DataFrames and asserts the recommendation + level math is correct.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ibkr_mcp_server import pivot


def make_bars(rows):
    """rows: list of (high, low, close). Returns a DataFrame."""
    return pd.DataFrame(rows, columns=["high", "low", "close"])


class TestPivotMath:
    def test_pivot_low_is_min_of_lows(self):
        bars = make_bars([(105, 100, 102), (104, 98, 99), (103, 99, 101)])
        a = pivot.analyze_pivot_loop(bars)
        assert a.pivot_low == 98.0
        assert a.pivot_high == 105.0

    def test_avg_close_to_low_matches_math(self):
        # (102-100) + (99-98) + (101-99) = 2 + 1 + 2 = 5 / 3 ≈ 1.6667
        bars = make_bars([(105, 100, 102), (104, 98, 99), (103, 99, 101)])
        a = pivot.analyze_pivot_loop(bars)
        assert a.avg_close_to_low == pytest.approx(5 / 3, abs=0.01)

    def test_avg_daily_range_matches_math(self):
        # (5 + 6 + 4) / 3 = 5
        bars = make_bars([(105, 100, 102), (104, 98, 99), (103, 99, 101)])
        a = pivot.analyze_pivot_loop(bars)
        assert a.avg_daily_range == pytest.approx(5.0)

    def test_suggested_entry_is_pivot_low_plus_buffer(self):
        bars = make_bars([(105, 100, 102), (104, 98, 99), (103, 99, 101)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.005)
        # 98 × 1.005 = 98.49
        assert a.suggested_entry == pytest.approx(98.49)

    def test_suggested_stop_is_pivot_low_minus_buffer(self):
        bars = make_bars([(105, 100, 102), (104, 98, 99), (103, 99, 101)])
        a = pivot.analyze_pivot_loop(bars, stop_buffer_pct=0.03)
        # 98 × 0.97 = 95.06
        assert a.suggested_stop == pytest.approx(95.06)


class TestRecommendation:
    def _at_entry_bars(self):
        # last close = 100 (the pivot low) -> distance_from_low = 0
        return make_bars([(110, 100, 105), (108, 100, 104), (107, 100, 100)])

    def test_below_entry_recommends_buy(self):
        bars = self._at_entry_bars()
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.01)
        # entry = 100 * 1.01 = 101; current = 100 < 101 → BUY
        assert a.recommendation.startswith("BUY")

    def test_close_to_entry_recommends_wait(self):
        # current sits between entry and 2× the buffer above pivot
        bars = make_bars([(110, 100, 105), (108, 100, 104), (107, 100, 101.5)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.01)
        # entry = 101; current = 101.5; 2× buffer = 102 → WAIT
        assert a.recommendation.startswith("WAIT")

    def test_above_target_recommends_sell(self):
        # Need: current >= entry + close_to_low rise
        # Pivot low 100, avg close-to-low ~5, entry ~100.5, target ~105.5
        bars = make_bars([(108, 100, 105), (107, 100, 105), (110, 100, 109)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.005)
        assert a.recommendation.startswith("SELL"), f"got: {a.recommendation}"

    def test_between_entry_and_target_recommends_hold(self):
        bars = make_bars([(108, 100, 102), (107, 100, 103), (106, 100, 103)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.005)
        # entry ~100.5, target ~ entry + close_to_low (~2.67) ≈ 103.17
        # current = 103 → HOLD
        assert a.recommendation.startswith("HOLD")


class TestCatalystGate:
    def _bars(self):
        return make_bars([(110, 100, 105), (108, 100, 104), (107, 100, 105)])

    def test_catalyst_within_horizon_blocks_with_exit_recommendation(self):
        bars = self._bars()
        catalysts = [{
            "type": "earnings",
            "date": "2026-07-29",
            "days_away": 1,
        }]
        a = pivot.analyze_pivot_loop(bars, catalysts, catalyst_horizon_days=2)
        assert a.blocked_by_catalyst is True
        assert a.recommendation.startswith("EXIT")
        assert any("earnings" in n for n in a.notes)

    def test_catalyst_outside_horizon_does_not_block(self):
        bars = self._bars()
        catalysts = [{
            "type": "earnings",
            "date": "2026-08-29",
            "days_away": 30,
        }]
        a = pivot.analyze_pivot_loop(bars, catalysts, catalyst_horizon_days=2)
        assert a.blocked_by_catalyst is False
        assert not a.recommendation.startswith("EXIT")
        assert a.days_to_next_catalyst == 30

    def test_no_catalysts_clean_path(self):
        bars = self._bars()
        a = pivot.analyze_pivot_loop(bars, catalysts=None)
        assert a.blocked_by_catalyst is False
        assert a.catalysts == []
        assert a.days_to_next_catalyst is None

    def test_past_catalysts_ignored(self):
        bars = self._bars()
        # negative days_away means "already happened" -- shouldn't affect anything
        catalysts = [{"type": "earnings", "date": "2025-04-01", "days_away": -30}]
        a = pivot.analyze_pivot_loop(bars, catalysts)
        assert a.blocked_by_catalyst is False
        assert a.catalysts == []  # filtered out


class TestEdgeCases:
    def test_empty_bars_raises(self):
        with pytest.raises(ValueError, match="at least 2 bars"):
            pivot.analyze_pivot_loop(make_bars([(100, 100, 100)]))

    def test_missing_columns_raises(self):
        # Two rows so we get past the bar-count check and actually hit
        # the column-shape check.
        bars = pd.DataFrame([[1, 2], [3, 4]], columns=["high", "low"])  # no close
        with pytest.raises(ValueError, match="missing required columns"):
            pivot.analyze_pivot_loop(bars)

    def test_flat_price_no_div_by_zero(self):
        bars = make_bars([(100, 100, 100), (100, 100, 100), (100, 100, 100)])
        a = pivot.analyze_pivot_loop(bars)
        assert a.avg_daily_range == 0.0
        assert a.avg_close_to_low == 0.0
        assert a.recommendation  # should still produce SOME recommendation

    def test_to_json_dict_serializable(self):
        bars = make_bars([(110, 100, 105), (108, 100, 104), (107, 100, 105)])
        a = pivot.analyze_pivot_loop(bars)
        d = pivot.to_json_dict(a)
        assert "pivot_low" in d
        assert "recommendation" in d
        assert "catalysts" in d
        # JSON-serializable round-trip
        import json
        json.dumps(d)  # raises on non-serializable types
