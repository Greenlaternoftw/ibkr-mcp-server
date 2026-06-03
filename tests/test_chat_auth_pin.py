"""Tests for the PIN unlock rate-limiter + lockout.

A 4-digit numeric PIN has 10K combinations -- the throttle, not the
digit count, is what keeps it brute-force-safe. These tests verify
the throttle behaves correctly under the conditions that actually
matter in production:

  * First few failures pass through (just 401, not 429).
  * 5th failure in a 60s window flips to ``rate_limited``.
  * 10th failure in an hour flips to ``locked_out``.
  * Lockout state ignores additional failures (still locked).
  * A successful unlock CLEARS the failure history -- a near-miss
    attacker can't leave the next legitimate user holding the bag.
  * ntfy alert fires ONCE on transition to lockout, not on every
    subsequent failure within the same window.

State is process-global; ``reset()`` is called via fixture autouse so
tests don't pollute each other.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ibkr_mcp_server.chat import auth_pin


@pytest.fixture(autouse=True)
def _reset_state():
    """Wipe the in-memory deque + alert timestamp before AND after."""
    auth_pin.reset()
    yield
    auth_pin.reset()


# --- baseline behavior ----------------------------------------------------


class TestStatus:
    def test_initial_status_is_ok(self):
        assert auth_pin.status() == "ok"

    def test_few_failures_below_rate_threshold_stay_ok(self):
        # Threshold is 5 in 60s -- 4 is fine.
        for _ in range(4):
            auth_pin.record_failure()
        assert auth_pin.status() == "ok"


# --- rate-limit transition ------------------------------------------------


class TestRateLimit:
    def test_fifth_failure_in_window_triggers_rate_limit(self):
        # 4 failures -> still ok
        for _ in range(4):
            assert auth_pin.record_failure() == "ok"
        # 5th flips to rate_limited
        assert auth_pin.record_failure() == "rate_limited"
        assert auth_pin.status() == "rate_limited"

    def test_rate_limit_clears_after_window_passes(self, monkeypatch):
        """Once the 60s sliding window slides past the 5 burst, ``status``
        drops back to ok (assuming we haven't hit lockout threshold)."""
        # Fake the clock so we can fast-forward without sleeping.
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        for _ in range(5):
            auth_pin.record_failure()
        assert auth_pin.status() == "rate_limited"

        # Slide forward past the rate window.
        now[0] += auth_pin.RATE_WINDOW_SECONDS + 1
        # Still within the lockout window so failures stay in the deque,
        # but they're outside the rate window -> ok again.
        assert auth_pin.status() == "ok"


# --- lockout transition ---------------------------------------------------


class TestLockout:
    def test_tenth_failure_in_hour_triggers_lockout(self, monkeypatch):
        """Spread failures over enough wall time to dodge the rate-limit
        gate but stay inside the lockout window."""
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        # 10 failures spaced 30s apart -- never hits 5-in-60s window
        # boundary, but accumulates to lockout threshold.
        for i in range(9):
            assert auth_pin.record_failure() in ("ok", "rate_limited")
            now[0] += 30
        # 10th
        assert auth_pin.record_failure() == "locked_out"
        assert auth_pin.status() == "locked_out"

    def test_lockout_persists_even_after_no_new_attempts(self, monkeypatch):
        """The whole point of lockout: stops counting attempts and
        blocks for a full hour regardless of subsequent behavior."""
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        for _ in range(10):
            auth_pin.record_failure()
        assert auth_pin.status() == "locked_out"

        # Slide forward 30 minutes (less than lockout window). Still locked.
        now[0] += 30 * 60
        assert auth_pin.status() == "locked_out"

    def test_lockout_clears_after_lockout_window(self, monkeypatch):
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        for _ in range(10):
            auth_pin.record_failure()
        assert auth_pin.status() == "locked_out"

        # Slide past the full lockout window. All failures expire from
        # the deque -> ok.
        now[0] += auth_pin.LOCKOUT_WINDOW_SECONDS + 1
        assert auth_pin.status() == "ok"


# --- success path ---------------------------------------------------------


class TestSuccess:
    def test_success_clears_failure_history(self):
        for _ in range(4):
            auth_pin.record_failure()
        # On the edge of rate-limit but still ok
        assert auth_pin.status() == "ok"

        auth_pin.record_success()
        assert auth_pin.status() == "ok"
        # Now we can absorb a full 4 more failures before any throttling
        for _ in range(4):
            assert auth_pin.record_failure() == "ok"


# --- ntfy alert -----------------------------------------------------------


class TestAlerts:
    def test_alert_fires_on_transition_to_lockout(self, monkeypatch):
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        with patch.object(auth_pin.notify, "send") as send:
            for _ in range(10):
                if auth_pin.record_failure() == "locked_out":
                    auth_pin.maybe_alert_lockout()
        assert send.call_count == 1
        # Title and priority are operator-visible -- sanity-check them.
        # notify.send is called with kwargs (title=..., message=..., priority=...).
        call = send.call_args
        title = call.kwargs.get("title") or (call.args[0] if call.args else "")
        assert "PIN" in title, f"title should mention PIN, got: {title!r}"
        assert call.kwargs.get("priority", 0) >= 4

    def test_alert_does_not_repeat_within_same_lockout_window(self, monkeypatch):
        """A sustained attack mustn't spam the operator's phone -- one
        push per lockout episode, not per failed attempt."""
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        with patch.object(auth_pin.notify, "send") as send:
            # Cross lockout threshold and call alert 20 times.
            for _ in range(20):
                auth_pin.record_failure()
                auth_pin.maybe_alert_lockout()
        # Only one push.
        assert send.call_count == 1

    def test_alert_rearms_after_window_passes(self, monkeypatch):
        """If the lockout fully expires and a new lockout happens later,
        the alert SHOULD fire again."""
        now = [1000000.0]
        monkeypatch.setattr(auth_pin.time, "time", lambda: now[0])

        with patch.object(auth_pin.notify, "send") as send:
            for _ in range(10):
                auth_pin.record_failure()
            auth_pin.maybe_alert_lockout()
            assert send.call_count == 1

            # Slide forward past the lockout window.
            now[0] += auth_pin.LOCKOUT_WINDOW_SECONDS + 1
            # Trigger a fresh lockout
            for _ in range(10):
                auth_pin.record_failure()
            auth_pin.maybe_alert_lockout()
        assert send.call_count == 2
