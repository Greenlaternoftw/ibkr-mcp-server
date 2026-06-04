"""Tests for the Command-Center backend endpoints.

Watchlist CRUD + user_prefs + a smoke test that the routes register
without import errors. The HTML side is exercised by the smoke test
in CI (just verifying the file is served); detailed UI behavior is
verified manually in Safari per the deployment runbook.

We don't test market_quote or research_symbol in unit tests -- those
need a live IBKR Gateway / live Anthropic API key respectively, so
they're covered via the in-browser /chat/api/diagnose flow on the VPS.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ibkr_mcp_server.chat.persistence import ChatStore


@pytest.fixture
def store(tmp_path: Path) -> ChatStore:
    return ChatStore(tmp_path / "chat.db")


# --- watchlists -----------------------------------------------------------


class TestWatchlists:
    def test_empty_store_lists_no_watchlists(self, store):
        assert store.list_watchlists() == []

    def test_create_then_list(self, store):
        wl = store.create_watchlist("Tech")
        assert wl["name"] == "Tech"
        assert wl["stock_count"] == 0
        rows = store.list_watchlists()
        assert len(rows) == 1
        assert rows[0]["name"] == "Tech"

    def test_create_duplicate_name_raises(self, store):
        store.create_watchlist("Tech")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_watchlist("Tech")

    def test_delete_watchlist(self, store):
        wl = store.create_watchlist("Doomed")
        assert store.delete_watchlist(wl["id"])
        assert store.list_watchlists() == []

    def test_delete_cascades_to_stocks(self, store):
        wl = store.create_watchlist("Cascade")
        store.add_watchlist_stock(wl["id"], "AAPL")
        store.add_watchlist_stock(wl["id"], "TSLA")
        assert len(store.get_watchlist_stocks(wl["id"])) == 2
        store.delete_watchlist(wl["id"])
        # Stocks should be gone via ON DELETE CASCADE.
        assert store.get_watchlist_stocks(wl["id"]) == []


# --- watchlist_stocks -----------------------------------------------------


class TestWatchlistStocks:
    def test_add_then_list(self, store):
        wl = store.create_watchlist("Trading")
        s = store.add_watchlist_stock(wl["id"], "nvda")  # lowercase input
        assert s["symbol"] == "NVDA"   # normalized
        listed = store.get_watchlist_stocks(wl["id"])
        assert len(listed) == 1
        assert listed[0]["symbol"] == "NVDA"

    def test_duplicate_symbol_in_same_list_rejected(self, store):
        wl = store.create_watchlist("X")
        store.add_watchlist_stock(wl["id"], "AAPL")
        with pytest.raises(sqlite3.IntegrityError):
            store.add_watchlist_stock(wl["id"], "AAPL")

    def test_same_symbol_in_two_lists_is_fine(self, store):
        a = store.create_watchlist("A")
        b = store.create_watchlist("B")
        store.add_watchlist_stock(a["id"], "AAPL")
        store.add_watchlist_stock(b["id"], "AAPL")
        assert len(store.get_watchlist_stocks(a["id"])) == 1
        assert len(store.get_watchlist_stocks(b["id"])) == 1

    def test_upsert_metrics(self, store):
        wl = store.create_watchlist("Metrics")
        store.add_watchlist_stock(wl["id"], "AAPL")
        # First write: rating + price
        out = store.upsert_watchlist_stock(
            wl["id"], "AAPL",
            rating="BUY", current_price=189.50, target_price=210.0,
        )
        assert out["rating"] == "BUY"
        assert out["current_price"] == 189.50
        # Partial update: only target moves; rating + current_price preserved
        out2 = store.upsert_watchlist_stock(wl["id"], "AAPL", target_price=215.0)
        assert out2["rating"] == "BUY"
        assert out2["current_price"] == 189.50
        assert out2["target_price"] == 215.0

    def test_upsert_unknown_symbol_returns_none(self, store):
        wl = store.create_watchlist("U")
        result = store.upsert_watchlist_stock(wl["id"], "NOPE", rating="BUY")
        assert result is None

    def test_remove(self, store):
        wl = store.create_watchlist("R")
        store.add_watchlist_stock(wl["id"], "AAPL")
        assert store.remove_watchlist_stock(wl["id"], "AAPL")
        assert store.get_watchlist_stocks(wl["id"]) == []
        # Removing again returns False.
        assert not store.remove_watchlist_stock(wl["id"], "AAPL")


# --- user_prefs -----------------------------------------------------------


class TestUserPrefs:
    def test_empty(self, store):
        assert store.get_pref("anything") is None
        assert store.list_prefs() == {}

    def test_set_then_get(self, store):
        store.set_pref("activePortfolio", "Watchlist")
        assert store.get_pref("activePortfolio") == "Watchlist"

    def test_set_overwrites(self, store):
        store.set_pref("threshold", "85")
        store.set_pref("threshold", "92")
        assert store.get_pref("threshold") == "92"

    def test_list_prefs(self, store):
        store.set_pref("a", "1")
        store.set_pref("b", "2")
        assert store.list_prefs() == {"a": "1", "b": "2"}

    def test_delete(self, store):
        store.set_pref("k", "v")
        assert store.delete_pref("k")
        assert store.get_pref("k") is None

    def test_oversized_key_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_pref("x" * 257, "v")

    def test_oversized_value_rejected(self, store):
        with pytest.raises(ValueError):
            store.set_pref("k", "x" * (65_536 + 1))


# --- route registration smoke test ---------------------------------------


class TestRoutesRegistered:
    def test_command_center_routes_all_present(self):
        """Sanity: all the new endpoints register without import errors."""
        from ibkr_mcp_server.chat.routes import chat_routes
        routes = chat_routes()
        paths = {
            (tuple(sorted(r.methods or [])), r.path)
            for r in routes
            if hasattr(r, "path") and hasattr(r, "methods")
        }
        # Spot-check the ones we just added.
        expected = [
            (("GET", "HEAD"), "/chat/api/account/summary"),
            (("GET", "HEAD"), "/chat/api/positions"),
            (("GET", "HEAD"), "/chat/api/watchlists"),
            (("POST",), "/chat/api/watchlists"),
            (("DELETE",), "/chat/api/watchlists/{wid}"),
            (("GET", "HEAD"), "/chat/api/watchlists/{wid}/stocks"),
            (("POST",), "/chat/api/watchlists/{wid}/stocks"),
            (("PATCH",), "/chat/api/watchlists/{wid}/stocks/{symbol}"),
            (("DELETE",), "/chat/api/watchlists/{wid}/stocks/{symbol}"),
            (("GET", "HEAD"), "/chat/api/market/quote"),
            (("GET", "HEAD"), "/chat/api/research/{symbol}"),
            (("GET", "HEAD"), "/chat/api/prefs"),
            (("POST",), "/chat/api/prefs"),
            (("DELETE",), "/chat/api/prefs/{key}"),
        ]
        for methods, path in expected:
            assert (methods, path) in paths, f"missing route: {methods} {path}"
