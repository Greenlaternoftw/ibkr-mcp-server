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
    # Note: these tests use FLAT-trend sample data (first close ≈ last
    # close within ±2%) so the trend-aware override doesn't fire and we
    # can isolate the pure price-vs-level recommendation logic.
    # Downtrend-overrides-BUY behavior is covered in TestTrendAdjustment.

    def test_below_entry_recommends_buy(self):
        # Flat trend (close stable at ~100), current = pivot_low
        bars = make_bars([(110, 100, 100), (108, 100, 100), (107, 100, 100)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.01)
        # entry = 100 × 1.01 = 101; current = 100 < 101 → BUY
        assert a.recommendation.startswith("BUY"), f"got: {a.recommendation}"

    def test_close_to_entry_recommends_wait(self):
        # Flat trend; current sits between entry and 2× buffer above pivot
        bars = make_bars([(110, 100, 101), (108, 100, 101), (107, 100, 101.5)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.01)
        # entry = 101; current = 101.5; 2× buffer = 102 → WAIT
        assert a.recommendation.startswith("WAIT"), f"got: {a.recommendation}"

    def test_above_target_recommends_sell(self):
        # Flat trend; current price clearly above target.
        # entry = 100 × 1.005 = 100.5; median_close_to_low = 6 -> target = 106.5
        # current = 107 > 106.5 → SELL
        bars = make_bars([(108, 100, 106), (107, 100, 106), (110, 100, 107)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.005)
        assert a.recommendation.startswith("SELL"), f"got: {a.recommendation}"

    def test_between_entry_and_target_recommends_hold(self):
        # Flat trend; current between entry and target
        bars = make_bars([(108, 100, 103), (107, 100, 103), (106, 100, 103)])
        a = pivot.analyze_pivot_loop(bars, entry_buffer_pct=0.005)
        # entry ~100.5, target ~ entry + median_close_to_low (~3) ≈ 103.5
        # current = 103 → HOLD
        assert a.recommendation.startswith("HOLD"), f"got: {a.recommendation}"


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


class TestTrendAdjustment:
    """The 'auto-adjust if price is trending' feature -- operator-requested."""

    def test_flat_trend_uses_raw_pivot_low(self):
        # All bars hover around the same price
        bars = make_bars([
            (101, 99, 100), (101, 99, 100), (102, 99, 100),
            (101, 99, 100), (102, 99, 101),
        ])
        a = pivot.analyze_pivot_loop(bars)
        assert a.trend_direction == "flat"
        assert a.effective_low == pytest.approx(99.0)
        assert "flat" in a.effective_low_source.lower()

    def test_uptrend_uses_trailing_3d_low_not_full_window(self):
        # Steady climb: window low is on day 1 (95), but trailing 3d
        # low is much higher (108).
        bars = make_bars([
            (96, 95, 95.5),    # window low here
            (100, 96, 99),
            (104, 100, 103),
            (108, 104, 107),
            (112, 108, 111),   # trailing-3 low starts here
            (116, 112, 115),
            (120, 116, 119),   # +25% over window -> strong uptrend
        ])
        a = pivot.analyze_pivot_loop(bars)
        assert a.trend_direction == "up"
        assert a.trend_strength == "strong"
        assert a.pivot_low == 95.0
        # Effective low should be trailing 3d min, which is 108
        assert a.effective_low == pytest.approx(108.0)
        assert "trailing" in a.effective_low_source.lower()
        # Suggested entry derives from effective_low, not pivot_low
        assert a.suggested_entry > 100  # would be ~95.5 if using pivot_low

    def test_downtrend_projects_floor_forward(self):
        # Steady decline: window low is today (90); slope says tomorrow
        # is probably lower.
        bars = make_bars([
            (108, 100, 105),
            (106, 98, 100),
            (102, 95, 97),
            (99, 92, 94),
            (96, 90, 91),   # -13% over window -> strong downtrend
        ])
        a = pivot.analyze_pivot_loop(bars)
        assert a.trend_direction == "down"
        assert a.trend_strength == "strong"
        assert a.pivot_low == 90.0
        # Effective low should be BELOW raw pivot_low (projecting drift)
        assert a.effective_low < a.pivot_low
        assert "projected" in a.effective_low_source.lower()

    def test_downtrend_recommendation_is_wait_not_buy_even_at_entry(self):
        # Downtrending + current price at pivot low -- old algo said BUY,
        # new algo says WAIT (don't catch a falling knife).
        bars = make_bars([
            (108, 100, 105),
            (106, 98, 100),
            (102, 95, 97),
            (99, 92, 94),
            (95, 90, 90),    # current = 90 = pivot_low; strong downtrend
        ])
        a = pivot.analyze_pivot_loop(bars)
        assert a.trend_direction == "down"
        assert a.recommendation.startswith("WAIT"), f"got: {a.recommendation}"
        assert "downtrend" in a.recommendation.lower()

    def test_strong_uptrend_past_target_returns_trending_not_sell(self):
        # Strong uptrend, price already ran past target -- old algo
        # said SELL, new algo says TRENDING (don't chase).
        bars = make_bars([
            (96, 95, 95.5),
            (100, 96, 99),
            (104, 100, 103),
            (108, 104, 107),
            (112, 108, 111),
            (116, 112, 115),
            (120, 116, 119),
        ])
        a = pivot.analyze_pivot_loop(bars)
        assert a.trend_direction == "up"
        assert a.trend_strength == "strong"
        # With effective_low ≈ 108 and current = 119, we're well past target
        assert a.recommendation.startswith("TRENDING"), f"got: {a.recommendation}"

    def test_trend_annotation_in_notes(self):
        bars = make_bars([
            (108, 100, 105),
            (106, 98, 100),
            (102, 95, 97),
            (99, 92, 94),
            (96, 90, 91),
        ])
        a = pivot.analyze_pivot_loop(bars)
        # Trend annotation should be in notes
        assert any("downtrend" in n.lower() for n in a.notes)

    def test_catalyst_block_overrides_uptrend_recommendation(self):
        # Even in a beautiful uptrend, an earnings call in 1 day = EXIT
        bars = make_bars([
            (96, 95, 95.5),
            (100, 96, 99),
            (104, 100, 103),
            (108, 104, 107),
            (112, 108, 111),
            (116, 112, 115),
            (120, 116, 119),
        ])
        catalysts = [{"type": "earnings", "date": "2026-07-29", "days_away": 1}]
        a = pivot.analyze_pivot_loop(bars, catalysts)
        assert a.trend_direction == "up"
        assert a.recommendation.startswith("EXIT")


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
