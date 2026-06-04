"""Tests for the Pivot Loop engine -- pure decision policy.

`pivot_loop.decide_next_action` is the policy core. It takes a loop-state
dict + a PivotAnalysis + two IBKR-derived bools and returns a Decision.
No I/O, fully unit-testable with fixtures.

The actual ticker (asyncio loop, IBKR calls, SQLite writes) is exercised
end-to-end on the live VPS deployment via the verify battery.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ibkr_mcp_server import pivot_loop


# ---------- fixtures -------------------------------------------------

def make_analysis(
    *,
    current_price=100.0,
    suggested_entry=100.0,
    suggested_stop=97.0,
    suggested_target=103.0,
    trend_direction="flat",
    trend_strength="weak",
    trend_pct_change=0.0,
    blocked_by_catalyst=False,
    days_to_next_catalyst=None,
):
    """Build a PivotAnalysis-shaped object (SimpleNamespace duck-types fine
    -- decide_next_action only reads attributes)."""
    return SimpleNamespace(
        current_price=current_price,
        suggested_entry=suggested_entry,
        suggested_stop=suggested_stop,
        suggested_target=suggested_target,
        trend_direction=trend_direction,
        trend_strength=trend_strength,
        trend_pct_change=trend_pct_change,
        blocked_by_catalyst=blocked_by_catalyst,
        days_to_next_catalyst=days_to_next_catalyst,
    )


def make_loop(
    *,
    status="waiting",
    initial_capital=1000.0,
    current_capital=1000.0,
    cumulative_realized=0.0,
    max_drawdown_pct=50.0,
    **extras,
):
    base = {
        "status": status,
        "initial_capital": initial_capital,
        "current_capital": current_capital,
        "cumulative_realized": cumulative_realized,
        "max_drawdown_pct": max_drawdown_pct,
        "compound": True,
    }
    base.update(extras)
    return base


# ---------- hard stops always win -----------------------------------

class TestHardStops:
    def test_drawdown_threshold_triggers_auto_stop(self):
        # 50% drawdown = $500 loss on $1000 initial
        loop = make_loop(cumulative_realized=-500.0)
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=0,
        )
        assert d.action == "auto_stop"
        assert "drawdown" in d.reason

    def test_drawdown_just_below_threshold_does_not_trigger(self):
        loop = make_loop(cumulative_realized=-499.0)
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=0,
        )
        assert d.action != "auto_stop"

    def test_consecutive_losses_triggers_auto_stop(self):
        loop = make_loop()
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=3,
        )
        assert d.action == "auto_stop"
        assert "consecutive" in d.reason

    def test_two_consecutive_losses_does_not_stop(self):
        loop = make_loop()
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=2,
        )
        assert d.action != "auto_stop"


# ---------- waiting → place_entry decision tree ---------------------

class TestWaiting:
    def test_catalyst_block_prevents_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(blocked_by_catalyst=True, days_to_next_catalyst=1)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "catalyst" in d.reason.lower()

    def test_strong_downtrend_prevents_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(trend_direction="down", trend_strength="strong",
                          trend_pct_change=-7.5)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "down" in d.reason.lower()

    def test_moderate_downtrend_prevents_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(trend_direction="down", trend_strength="moderate",
                          trend_pct_change=-3.5)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"

    def test_weak_downtrend_does_not_prevent_entry_if_price_at_entry(self):
        loop = make_loop(status="waiting")
        # Weak downtrend (<2% over window) + price at entry → BUY
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            trend_direction="flat",  # weak/flat
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_price_above_entry_threshold_skips(self):
        loop = make_loop(status="waiting")
        # Entry = 100, but current = 100.6 > 100 * 1.005 = 100.5 → skip
        a = make_analysis(current_price=100.6, suggested_entry=100.0)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert ">" in d.reason

    def test_price_below_buffer_enters(self):
        loop = make_loop(status="waiting")
        # Entry = 100, current = 100.4 (well below entry × 1.005 = 100.5) → BUY
        # (Avoid the exact-boundary 100.5 case -- 100.0 × 1.005 isn't
        # exactly 100.5 in IEEE 754, so equality there is fragile.)
        a = make_analysis(current_price=100.4, suggested_entry=100.0)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_open_position_unexpected_no_ops_safely(self):
        # Loop says waiting but IBKR shows we already hold the symbol
        # (operator may have entered manually). Don't double-enter.
        loop = make_loop(status="waiting")
        a = make_analysis()
        d = pivot_loop.decide_next_action(loop, a, has_open_position=True,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "position exists" in d.reason

    def test_entry_decision_carries_target_and_stop(self):
        loop = make_loop(status="waiting")
        a = make_analysis(current_price=100.0, suggested_entry=100.0,
                          suggested_target=103.5, suggested_stop=97.5)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"
        assert d.extra["target_price"] == 103.5
        assert d.extra["stop_price"] == 97.5


# ---------- entry_pending → place_oca / revert ----------------------

class TestEntryPending:
    def test_position_appears_attaches_oca(self):
        loop = make_loop(status="entry_pending")
        a = make_analysis(suggested_stop=97.0, suggested_target=103.0)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=True,
                                          last_3_cycles_losses=0)
        assert d.action == "place_oca"
        assert d.extra["stop_price"] == 97.0
        assert d.extra["target_price"] == 103.0

    def test_no_position_means_ioc_did_not_fill(self):
        loop = make_loop(status="entry_pending")
        a = make_analysis()
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert d.extra.get("revert_to_waiting") is True


# ---------- holding → catalyst-force-exit / record / monitor --------

class TestHolding:
    def test_position_gone_triggers_record_cycle(self):
        loop = make_loop(status="holding")
        a = make_analysis()
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "record_cycle"

    def test_inbound_catalyst_forces_exit_even_when_position_open(self):
        loop = make_loop(status="holding")
        a = make_analysis(blocked_by_catalyst=True, days_to_next_catalyst=1)
        d = pivot_loop.decide_next_action(loop, a, has_open_position=True,
                                          last_3_cycles_losses=0)
        assert d.action == "force_exit"
        assert "catalyst" in d.reason.lower()

    def test_holding_clean_path_just_monitors(self):
        loop = make_loop(status="holding")
        a = make_analysis()
        d = pivot_loop.decide_next_action(loop, a, has_open_position=True,
                                          last_3_cycles_losses=0)
        assert d.action == "monitor_holding"


# ---------- terminal states / idle paths ----------------------------

class TestIdleStates:
    def test_paused_returns_no_op(self):
        loop = make_loop(status="paused")
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=0,
        )
        assert d.action == "no_op"

    def test_stopped_returns_no_op(self):
        loop = make_loop(status="stopped")
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=False,
            last_3_cycles_losses=0,
        )
        assert d.action == "no_op"

    def test_exit_pending_returns_no_op(self):
        loop = make_loop(status="exit_pending")
        d = pivot_loop.decide_next_action(
            loop, make_analysis(), has_open_position=True,
            last_3_cycles_losses=0,
        )
        assert d.action == "no_op"


# ---------- market regime + tick interval ---------------------------

import datetime as dt


class TestMarketRegime:
    def test_rth_weekday_10am_et_is_rth(self):
        # 14:00 UTC on a Wednesday in summer = 10am ET (DST)
        when = dt.datetime(2026, 6, 3, 14, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is True

    def test_premarket_8am_et_is_oth(self):
        when = dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is False

    def test_afterhours_5pm_et_is_oth(self):
        when = dt.datetime(2026, 6, 3, 21, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is False

    def test_saturday_is_oth(self):
        when = dt.datetime(2026, 6, 6, 14, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is False

    def test_market_open_boundary_inclusive(self):
        # 9:30:00 ET = 13:30 UTC (DST) on a weekday
        when = dt.datetime(2026, 6, 3, 13, 30, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is True

    def test_market_close_boundary_exclusive(self):
        # 16:00:00 ET = 20:00 UTC (DST) -- close is exclusive
        when = dt.datetime(2026, 6, 3, 20, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.is_regular_trading_hours(when) is False

    def test_tick_interval_60s_during_rth(self):
        when = dt.datetime(2026, 6, 3, 14, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.current_tick_interval(when) == 60

    def test_tick_interval_300s_during_oth(self):
        when = dt.datetime(2026, 6, 6, 14, 0, tzinfo=dt.timezone.utc)
        assert pivot_loop.current_tick_interval(when) == 300
