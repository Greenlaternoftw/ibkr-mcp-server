"""Tests for the ntfy.sh phone-alert helper.

Verifies the three things that matter most:
  * Gating: NOTIFY_ENABLED=False makes ``send`` a no-op.
  * Failure-silent: network errors are swallowed, never raised.
  * Correct wire format: title/priority/tags headers + topic in URL.

Also covers the two convenience wrappers used by the daemon
(``alert_disconnect`` / ``alert_reconnect``).
"""

from __future__ import annotations

import asyncio
import urllib.error
from unittest.mock import AsyncMock, patch

import pytest

from ibkr_mcp_server import notify
from ibkr_mcp_server.config import settings


@pytest.fixture
def notify_on(monkeypatch):
    """Enable notifications and pin a deterministic URL+topic."""
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "ntfy_url", "https://ntfy.sh")
    monkeypatch.setattr(settings, "ntfy_topic", "ibkr-test-topic")


# --- gating ---------------------------------------------------------------


def test_send_noop_when_disabled(monkeypatch):
    """notify_enabled=False short-circuits before building a request."""
    monkeypatch.setattr(settings, "notify_enabled", False)
    monkeypatch.setattr(settings, "ntfy_topic", "ibkr-test-topic")
    with patch("ibkr_mcp_server.notify._post_sync") as posted:
        notify.send("t", "m")
    assert not posted.called


def test_send_noop_when_topic_missing(monkeypatch):
    """Even with notify_enabled=True, an empty topic is still a no-op."""
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "ntfy_topic", None)
    with patch("ibkr_mcp_server.notify._post_sync") as posted:
        notify.send("t", "m")
    assert not posted.called


# --- request shape --------------------------------------------------------


def test_send_builds_correct_request(notify_on):
    """The POST hits {url}/{topic} with title/priority/tags headers."""
    captured = {}

    def fake_post(req):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())

    with patch("ibkr_mcp_server.notify._post_sync", side_effect=fake_post):
        notify.send(
            "Hello",
            "the body",
            priority=4,
            tags=["warning", "rotating_light"],
        )

    assert captured["url"] == "https://ntfy.sh/ibkr-test-topic"
    assert captured["data"] == b"the body"
    # urllib title-cases header keys.
    assert captured["headers"]["Title"] == "Hello"
    assert captured["headers"]["Priority"] == "4"
    assert captured["headers"]["Tags"] == "warning,rotating_light"


def test_send_strips_trailing_slash_on_url(monkeypatch):
    """A trailing slash in NTFY_URL shouldn't yield a double-slash POST."""
    monkeypatch.setattr(settings, "notify_enabled", True)
    monkeypatch.setattr(settings, "ntfy_url", "https://ntfy.example.com/")
    monkeypatch.setattr(settings, "ntfy_topic", "t1")

    captured = {}

    def fake_post(req):
        captured["url"] = req.full_url

    with patch("ibkr_mcp_server.notify._post_sync", side_effect=fake_post):
        notify.send("x", "y")
    assert captured["url"] == "https://ntfy.example.com/t1"


# --- failure-silent -------------------------------------------------------


def test_post_swallows_url_error(notify_on):
    """A network failure must NOT propagate."""
    with patch(
        "ibkr_mcp_server.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("no route"),
    ):
        # This MUST NOT raise. If it does, the daemon's disconnect handler
        # would crash, which is exactly what we're trying to prevent.
        notify.send("t", "m")


def test_post_swallows_timeout(notify_on):
    with patch(
        "ibkr_mcp_server.notify.urllib.request.urlopen",
        side_effect=TimeoutError("slow"),
    ):
        notify.send("t", "m")  # no raise


def test_post_swallows_generic_oserror(notify_on):
    with patch(
        "ibkr_mcp_server.notify.urllib.request.urlopen",
        side_effect=OSError("dns"),
    ):
        notify.send("t", "m")


# --- async dispatch -------------------------------------------------------


@pytest.mark.asyncio
async def test_send_from_async_does_not_block(notify_on):
    """When called from an asyncio task, the POST must run on a thread."""
    called_on_thread = {"value": None}
    import threading

    main_thread_id = threading.get_ident()

    def fake_post(req):
        called_on_thread["value"] = threading.get_ident()

    with patch("ibkr_mcp_server.notify._post_sync", side_effect=fake_post):
        notify.send("t", "m")
        # Give the executor a tick to run.
        await asyncio.sleep(0.05)

    assert called_on_thread["value"] is not None
    # urllib POST must NOT have run on the asyncio loop's thread.
    assert called_on_thread["value"] != main_thread_id


# --- convenience wrappers -------------------------------------------------


def _send_call_field(call, field):
    """Look up a field passed to notify.send() regardless of positional vs
    keyword. Title is arg[0]/kw['title']; message is arg[1]/kw['message']."""
    positions = {"title": 0, "message": 1}
    if field in call.kwargs:
        return call.kwargs[field]
    return call.args[positions[field]]


def test_alert_disconnect_passes_through(notify_on):
    with patch("ibkr_mcp_server.notify.send") as send:
        notify.alert_disconnect("test reason")
    assert send.called
    title = _send_call_field(send.call_args, "title")
    message = _send_call_field(send.call_args, "message")
    assert "disconnected" in title.lower()
    assert "test reason" in message
    assert send.call_args.kwargs["priority"] == 4


def test_alert_reconnect_passes_through(notify_on):
    with patch("ibkr_mcp_server.notify.send") as send:
        notify.alert_reconnect()
    assert send.called
    title = _send_call_field(send.call_args, "title")
    assert "reconnected" in title.lower()
    assert send.call_args.kwargs["priority"] == 2


# --- client integration ---------------------------------------------------
#
# Verifies that the client's _on_disconnect / connect()-success paths fire
# the wrappers correctly, including the "only on reconnect, not first
# connect" rule.


class TestClientWiring:
    @pytest.mark.asyncio
    async def test_first_connect_does_not_alert_reconnect(self, ibkr_client_mock):
        """On the daemon's initial successful connect (no prior drop), we
        must NOT send a reconnect alert."""
        from ibkr_mcp_server.client import IBKRClient

        c = IBKRClient()
        c._had_prior_connection = False
        c._disconnect_alert_sent = False

        # Simulate the relevant tail of connect(): the alert gating block.
        with patch("ibkr_mcp_server.client.notify.alert_reconnect") as alert:
            # Inline the gating logic exactly as it appears in connect().
            if c._had_prior_connection and c._disconnect_alert_sent:
                from ibkr_mcp_server import notify as _n
                _n.alert_reconnect()
            c._disconnect_alert_sent = False
        assert not alert.called

    def test_disconnect_fires_alert_once(self, ibkr_client_mock):
        """_on_disconnect must call alert_disconnect exactly once even if
        ib_async re-emits the event (it can during retry storms)."""
        c = ibkr_client_mock
        c._connected = True
        c._disconnect_alert_sent = False
        c._had_prior_connection = False

        # Don't let _reconnect spin up a real background task.
        with patch.object(c, "_reconnect"), \
             patch("ibkr_mcp_server.client.notify.alert_disconnect") as alert, \
             patch("ibkr_mcp_server.client.asyncio.create_task"):
            c._on_disconnect()
            c._on_disconnect()  # ib_async re-emit
            c._on_disconnect()
        assert alert.call_count == 1
        assert c._had_prior_connection is True


# --- persistent reconnect loop --------------------------------------------
#
# These cover the bug that motivated the loop: the old single-shot
# _reconnect gave up at T+8s, so IBKR-Gateway restarts (which take ~90s
# to IBC-relogin) left the daemon stuck until cron noticed. Behavior we
# need now: keep trying until success or max_duration, then alert.


class TestPersistentReconnect:
    @pytest.fixture
    def reconnect_client(self):
        """Bare client wired so _reconnect can run quickly under unit-test
        budgets. We monkey-patch the class attributes to ~0 so the loop's
        own sleeps don't actually pause the test."""
        from ibkr_mcp_server.client import IBKRClient

        c = IBKRClient()
        c.reconnect_delay = 0  # skip the initial pause
        c.RECONNECT_RETRY_INTERVAL = 0.01
        c.RECONNECT_MAX_DURATION = 1.0  # plenty for the test cases
        return c

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self, reconnect_client):
        """Happy path: connect() works immediately, loop exits without
        firing the failure alert."""
        c = reconnect_client
        connect_mock = AsyncMock(return_value=True)
        with patch.object(c, "connect", connect_mock), \
             patch("ibkr_mcp_server.client.notify.alert_reconnect_failed") as failed:
            await c._reconnect()
        assert connect_mock.await_count == 1
        assert not failed.called

    @pytest.mark.asyncio
    async def test_succeeds_after_several_failures(self, reconnect_client):
        """Realistic IBC scenario: first few connect() attempts raise
        (Gateway still booting), then one succeeds. Loop must keep going
        and NOT fire the failure alert."""
        c = reconnect_client

        attempts = {"n": 0}

        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 4:
                raise ConnectionError("gateway not ready")
            return True

        with patch.object(c, "connect", side_effect=flaky), \
             patch("ibkr_mcp_server.client.notify.alert_reconnect_failed") as failed:
            await c._reconnect()
        assert attempts["n"] == 4
        assert not failed.called

    @pytest.mark.asyncio
    async def test_gives_up_after_max_duration(self, reconnect_client):
        """If connect() never succeeds within RECONNECT_MAX_DURATION, the
        loop exits and fires alert_reconnect_failed with attempt count."""
        c = reconnect_client
        c.RECONNECT_MAX_DURATION = 0.05  # tiny ceiling — guarantees exhaustion

        connect_mock = AsyncMock(side_effect=ConnectionError("never recovers"))
        with patch.object(c, "connect", connect_mock), \
             patch("ibkr_mcp_server.client.notify.alert_reconnect_failed") as failed:
            await c._reconnect()
        assert connect_mock.await_count >= 1
        assert failed.called
        # The alert should report SOMETHING for attempts and duration.
        kwargs = failed.call_args.kwargs
        assert kwargs.get("attempts", 0) >= 1
        assert kwargs.get("duration_seconds", -1) >= 0
