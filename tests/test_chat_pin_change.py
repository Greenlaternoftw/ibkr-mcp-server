"""Tests for the PIN-change UX.

Two layers under test:

  * ChatStore.{get_pin, set_pin, clear_pin} -- the SQLite-backed
    storage that lets the PIN survive daemon restarts without an .env
    edit.
  * routes._effective_pin() resolution order: SQLite wins, env-var
    falls through as the seed.

The full HTTP endpoint flow (auth + throttle + change) is covered by
the test_chat_auth_pin tests for the throttle pieces and by manual
testing for the endpoint itself; this file focuses on the persistence
+ resolution that's NEW to this commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_mcp_server.chat.persistence import ChatStore


@pytest.fixture
def store(tmp_path: Path) -> ChatStore:
    return ChatStore(tmp_path / "chat.db")


# --- PIN storage round-trip ----------------------------------------------


class TestPinStorage:
    def test_empty_store_returns_none(self, store: ChatStore):
        assert store.get_pin() is None

    def test_set_and_get_round_trip(self, store: ChatStore):
        store.set_pin("482917")
        assert store.get_pin() == "482917"

    def test_set_overwrites_previous_pin(self, store: ChatStore):
        store.set_pin("1111")
        store.set_pin("2222")
        assert store.get_pin() == "2222"

    def test_clear_removes_pin(self, store: ChatStore):
        store.set_pin("1234")
        store.clear_pin()
        assert store.get_pin() is None

    def test_pin_survives_store_recreation(self, tmp_path: Path):
        """The persistence guarantee: changing the PIN once via the UI
        should mean every subsequent daemon process reads the new
        value, not the stale CHAT_PIN env var."""
        path = tmp_path / "chat.db"
        s1 = ChatStore(path)
        s1.set_pin("5555")
        # New ChatStore instance pointing at the same file
        s2 = ChatStore(path)
        assert s2.get_pin() == "5555"

    def test_alphanumeric_pin_works(self, store: ChatStore):
        """Server accepts any string, not just digits."""
        store.set_pin("trade2026")
        assert store.get_pin() == "trade2026"


# --- effective-PIN resolution -------------------------------------------
#
# Tests the routes._effective_pin() helper that picks SQLite over .env.


class TestEffectivePin:
    def _patch_routes_store(self, monkeypatch, store):
        """Replace the singleton ChatStore for the test."""
        from ibkr_mcp_server.chat import routes as _routes
        _routes._store = store

    def test_sqlite_pin_wins_when_set(self, monkeypatch, store):
        """If SQLite has a PIN, .env CHAT_PIN is ignored. This is the
        whole point of letting the user change PIN without an .env edit."""
        from ibkr_mcp_server.chat import routes as _routes
        from ibkr_mcp_server.config import settings as _settings

        self._patch_routes_store(monkeypatch, store)
        monkeypatch.setattr(_settings, "chat_pin", "9999")  # env value
        store.set_pin("1234")                                # SQLite value

        # SQLite wins.
        assert _routes._effective_pin() == "1234"

    def test_env_falls_through_when_sqlite_empty(self, monkeypatch, store):
        """If SQLite is empty (initial deploy), the .env CHAT_PIN is
        the seed. This is what makes the initial setup work without
        requiring the user to also visit a UI."""
        from ibkr_mcp_server.chat import routes as _routes
        from ibkr_mcp_server.config import settings as _settings

        self._patch_routes_store(monkeypatch, store)
        monkeypatch.setattr(_settings, "chat_pin", "9999")
        # SQLite intentionally empty

        assert _routes._effective_pin() == "9999"

    def test_neither_set_returns_none(self, monkeypatch, store):
        """No SQLite, no .env -> PIN unlock UX is disabled."""
        from ibkr_mcp_server.chat import routes as _routes
        from ibkr_mcp_server.config import settings as _settings

        self._patch_routes_store(monkeypatch, store)
        monkeypatch.setattr(_settings, "chat_pin", None)

        assert _routes._effective_pin() is None

    def test_clear_pin_falls_back_to_env(self, monkeypatch, store):
        """If the operator clears the SQLite PIN (via change endpoint
        setting it back to the env value, then... we don't have a
        clear endpoint, but conceptually): clear_pin should make the
        env-var the active source again."""
        from ibkr_mcp_server.chat import routes as _routes
        from ibkr_mcp_server.config import settings as _settings

        self._patch_routes_store(monkeypatch, store)
        monkeypatch.setattr(_settings, "chat_pin", "9999")
        store.set_pin("1234")
        assert _routes._effective_pin() == "1234"

        store.clear_pin()
        assert _routes._effective_pin() == "9999"
