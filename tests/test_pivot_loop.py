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
    # Phase C+E gate fields. Default = unknown (None) so existing
    # tests that don't care about volume/regime get the no-gate
    # behavior they expect.
    volume_ok=None,
    volume_ratio=None,
    market_regime_enabled=None,
    vol_ok=None,
    vol_ratio=None,
    news_sentiment_ok=None,
    news_score=None,
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
        volume_ok=volume_ok,
        volume_ratio=volume_ratio,
        market_regime_enabled=market_regime_enabled,
        vol_ok=vol_ok,
        vol_ratio=vol_ratio,
        news_sentiment_ok=news_sentiment_ok,
        news_score=news_score,
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


# ---------- Phase C+E engine gates ----------------------------------
#
# The pivot.py analysis layer already gates the RECOMMENDATION text on
# volume + regime; these tests pin down that decide_next_action ALSO
# honors them when picking the engine's action. Without these, the
# dashboard would say "WAIT - risk-off" but the engine would still
# auto-enter on the next tick.

class TestEngineHonorsRegimeAndVolume:
    def test_regime_risk_off_blocks_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            market_regime_enabled=False,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "regime" in d.reason.lower()

    def test_regime_risk_on_allows_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            market_regime_enabled=True,
            volume_ok=True,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_regime_unknown_does_not_block(self):
        # None → fetch failed; skip the gate (don't lock the operator
        # out of trading just because SPY data was momentarily unfetchable)
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            market_regime_enabled=None,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_low_volume_blocks_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            volume_ok=False, volume_ratio=0.55,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "volume" in d.reason.lower()
        assert "0.55" in d.reason

    def test_volume_unknown_does_not_block(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            volume_ok=None,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_catalyst_block_takes_precedence_over_regime(self):
        # Catalyst > regime > volume in the precedence ordering
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            blocked_by_catalyst=True, days_to_next_catalyst=1,
            market_regime_enabled=False,
            volume_ok=False, volume_ratio=0.5,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "catalyst" in d.reason.lower()

    def test_vol_expansion_blocks_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            vol_ok=False, vol_ratio=1.85,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "vol" in d.reason.lower()
        assert "1.85" in d.reason

    def test_vol_unknown_does_not_block(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            vol_ok=None,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_regime_takes_precedence_over_vol_expansion(self):
        # Regime check fires first in the precedence ordering
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            market_regime_enabled=False,
            vol_ok=False, vol_ratio=2.0,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "regime" in d.reason.lower()

    def test_negative_news_blocks_entry(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            news_sentiment_ok=False, news_score=-7,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        assert "news" in d.reason.lower()

    def test_neutral_news_does_not_block(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            news_sentiment_ok=True, news_score=2,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_news_unknown_does_not_block(self):
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            news_sentiment_ok=None,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "place_entry"

    def test_downtrend_blocks_before_regime_check(self):
        # Downtrend guard fires before regime gate
        loop = make_loop(status="waiting")
        a = make_analysis(
            current_price=100.0, suggested_entry=100.0,
            trend_direction="down", trend_strength="strong",
            trend_pct_change=-7.5,
            market_regime_enabled=False,
        )
        d = pivot_loop.decide_next_action(loop, a, has_open_position=False,
                                          last_3_cycles_losses=0)
        assert d.action == "no_op"
        # Operator sees the trend reason (precedence) not the regime
        assert "down" in d.reason.lower()


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
