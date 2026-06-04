"""Tests for live-trading safety rails.

The breaker logic is pure (state in a module-level dataclass);
no IBKR/network mocking needed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ibkr_mcp_server import live_safety as ls
from ibkr_mcp_server.config import settings


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Each test gets a fresh breaker state."""
    ls._breaker.tripped = False
    ls._breaker.tripped_at = None
    ls._breaker.tripped_at_pnl = None
    ls._breaker.reset_for_date = None
    yield
    ls._breaker.tripped = False
    ls._breaker.tripped_at = None
    ls._breaker.tripped_at_pnl = None
    ls._breaker.reset_for_date = None


@pytest.fixture
def live_mode(monkeypatch):
    """Force live mode for the duration of the test."""
    monkeypatch.setattr(settings, "ibkr_is_paper", False)
    yield


@pytest.fixture
def paper_mode(monkeypatch):
    """Force paper mode."""
    monkeypatch.setattr(settings, "ibkr_is_paper", True)
    yield


class TestIsLiveMode:
    def test_paper_mode_detected(self, paper_mode):
        assert ls.is_live_mode() is False

    def test_live_mode_detected(self, live_mode):
        assert ls.is_live_mode() is True


class TestEffectiveMaxOrderSize:
    def test_paper_uses_default(self, paper_mode, monkeypatch):
        monkeypatch.setattr(settings, "max_order_size", 1000)
        monkeypatch.setattr(settings, "live_max_order_size", 100)
        assert ls.effective_max_order_size() == 1000

    def test_live_uses_smaller_cap(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "max_order_size", 1000)
        monkeypatch.setattr(settings, "live_max_order_size", 100)
        assert ls.effective_max_order_size() == 100

    def test_live_caps_at_global_max_if_lower(self, live_mode, monkeypatch):
        # If operator dropped max_order_size below live_max_order_size,
        # we honor the lower one.
        monkeypatch.setattr(settings, "max_order_size", 50)
        monkeypatch.setattr(settings, "live_max_order_size", 100)
        assert ls.effective_max_order_size() == 50


class TestCircuitBreakerInPaperMode:
    def test_paper_mode_never_trips(self, paper_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -100.0)
        # Even with massive losses, paper mode breaker doesn't fire
        assert ls.check_daily_pnl_breaker(-1_000_000.0) is False
        assert ls.get_breaker_state().tripped is False


class TestCircuitBreakerInLiveMode:
    def test_above_limit_does_not_trip(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        # Down $100, but not yet at -$500
        assert ls.check_daily_pnl_breaker(-100.0) is False
        assert ls.get_breaker_state().tripped is False

    def test_at_threshold_trips(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        # Exactly at the floor -- trips
        assert ls.check_daily_pnl_breaker(-500.0) is True
        assert ls.get_breaker_state().tripped is True
        assert ls.get_breaker_state().tripped_at_pnl == -500.0

    def test_below_threshold_trips(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        assert ls.check_daily_pnl_breaker(-700.0) is True
        assert ls.get_breaker_state().tripped is True

    def test_once_tripped_stays_tripped_same_day(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        # Trip the breaker
        assert ls.check_daily_pnl_breaker(-700.0) is True
        # P&L recovers above the floor -- breaker STAYS tripped
        # (the rail is "stop trading after a bad day", not "auto-resume
        # if it bounces back")
        assert ls.check_daily_pnl_breaker(-100.0) is True
        assert ls.get_breaker_state().tripped is True

    def test_new_day_resets(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        # Trip on day 1
        day1 = dt.date(2026, 6, 4)
        assert ls.check_daily_pnl_breaker(-700.0, today=day1) is True
        # Day 2 with clean P&L -- resets, not tripped
        day2 = dt.date(2026, 6, 5)
        assert ls.check_daily_pnl_breaker(0.0, today=day2) is False
        assert ls.get_breaker_state().tripped is False

    def test_manual_reset(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        ls.check_daily_pnl_breaker(-700.0)
        assert ls.get_breaker_state().tripped is True
        ls.manual_reset_breaker()
        assert ls.get_breaker_state().tripped is False
        # And we can trip again if we lose more
        assert ls.check_daily_pnl_breaker(-700.0) is True


class TestStatusDict:
    def test_paper_mode_status(self, paper_mode):
        s = ls.status_dict()
        assert s["live_mode"] is False
        assert s["daily_loss_limit"] is None
        assert s["breaker_tripped"] is False

    def test_live_mode_status(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        s = ls.status_dict()
        assert s["live_mode"] is True
        assert s["daily_loss_limit"] == -500.0
        assert s["breaker_tripped"] is False

    def test_status_reflects_trip(self, live_mode, monkeypatch):
        monkeypatch.setattr(settings, "live_daily_loss_limit", -500.0)
        ls.check_daily_pnl_breaker(-700.0)
        s = ls.status_dict()
        assert s["breaker_tripped"] is True
        assert s["breaker_tripped_at_pnl"] == -700.0
