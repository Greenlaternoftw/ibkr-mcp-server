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


# --- append_message (fills-into-chat bridge) -----------------------------


class TestAppendMessage:
    def test_append_to_existing_thread(self, store):
        thread = store.create_thread("test")
        ok = store.append_message(thread["id"], role="user", content="🟢 FILL · BUY 100 AAPL @ $310.50 · order #42")
        assert ok is True
        msgs = store.get_messages(thread["id"])
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert "FILL" in msgs[0]["content"]
        assert "order #42" in msgs[0]["content"]

    def test_append_preserves_existing_messages(self, store):
        thread = store.create_thread("test")
        # Seed with a normal conversation
        store.replace_messages(thread["id"], [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        # Append a fill notification
        store.append_message(thread["id"], role="user", content="🟢 FILL · BUY 100 AAPL @ $310.50")
        msgs = store.get_messages(thread["id"])
        assert len(msgs) == 3
        # Existing messages intact
        assert msgs[0]["content"] == "hi"
        assert msgs[1]["content"] == "hello"
        # New one appended at the end
        assert "FILL" in msgs[2]["content"]

    def test_append_to_missing_thread_returns_false(self, store):
        ok = store.append_message("thr_nope", role="user", content="x")
        assert ok is False

    def test_append_rejects_unknown_role(self, store):
        thread = store.create_thread("test")
        with pytest.raises(ValueError, match="invalid role"):
            store.append_message(thread["id"], role="bogus", content="x")

    def test_append_bumps_thread_updated_at(self, store):
        import time as _time
        thread = store.create_thread("test")
        original_updated = store.get_thread(thread["id"])["updated_at"]
        _time.sleep(0.01)  # ensure microsecond clock ticks
        store.append_message(thread["id"], role="user", content="ping")
        new_updated = store.get_thread(thread["id"])["updated_at"]
        assert new_updated > original_updated


# --- pivot loops ---------------------------------------------------------


class TestPivotLoops:
    def test_create_then_get(self, store):
        loop = store.create_pivot_loop(
            "AAPL", initial_capital=1000.0, lookback_days=7,
            entry_price=305.0, target_price=315.0, stop_price=300.0,
        )
        assert loop["symbol"] == "AAPL"
        assert loop["status"] == "waiting"
        assert loop["initial_capital"] == 1000.0
        assert loop["current_capital"] == 1000.0
        assert loop["compound"] is True
        got = store.get_pivot_loop("aapl")  # case-insensitive
        assert got["id"] == loop["id"]

    def test_create_duplicate_symbol_rejected(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        with pytest.raises(sqlite3.IntegrityError):
            store.create_pivot_loop("AAPL", 2000.0, 14)

    def test_create_validation(self, store):
        with pytest.raises(ValueError, match="symbol required"):
            store.create_pivot_loop("", 1000.0, 7)
        with pytest.raises(ValueError, match="initial_capital"):
            store.create_pivot_loop("AAPL", 50.0, 7)
        with pytest.raises(ValueError, match="lookback_days"):
            store.create_pivot_loop("AAPL", 1000.0, 200)

    def test_list_excludes_stopped_by_default(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        store.create_pivot_loop("F", 500.0, 14)
        store.stop_pivot_loop("F")
        active = store.list_pivot_loops()
        assert {l["symbol"] for l in active} == {"AAPL"}
        all_loops = store.list_pivot_loops(include_stopped=True)
        assert {l["symbol"] for l in all_loops} == {"AAPL", "F"}

    def test_update_allowed_field(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        out = store.update_pivot_loop("AAPL", status="holding", current_shares=10)
        assert out["status"] == "holding"
        assert out["current_shares"] == 10

    def test_update_rejects_unknown_field(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        with pytest.raises(ValueError, match="non-updatable fields"):
            store.update_pivot_loop("AAPL", initial_capital=5000)  # immutable

    def test_stop_marks_stopped_preserving_row(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        out = store.stop_pivot_loop("AAPL")
        assert out["status"] == "stopped"
        assert out["stopped_at"] is not None
        # Row still queryable
        assert store.get_pivot_loop("AAPL")["status"] == "stopped"

    def test_stop_idempotent_returns_none_second_time(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        store.stop_pivot_loop("AAPL")
        # Second stop is a no-op (row already stopped, can't transition again)
        assert store.stop_pivot_loop("AAPL") is None

    def test_record_cycle_updates_roll_ups_win(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        out = store.record_pivot_loop_cycle(
            "AAPL",
            capital_at_start=1000.0,
            entry_price=305.0, entry_fill=305.10, entry_at="2026-06-03T14:00:00Z",
            shares=3, exit_fill=310.50, exit_at="2026-06-03T18:30:00Z",
            exit_reason="target", realized_pnl=16.20,
        )
        assert out["cycle_count"] == 1
        assert out["win_count"] == 1
        assert out["loss_count"] == 0
        assert out["cumulative_realized"] == pytest.approx(16.20)
        # Compounding ON by default -> current_capital grows by realized P&L
        assert out["current_capital"] == pytest.approx(1016.20)
        # Position cleared, status back to waiting
        assert out["current_shares"] == 0
        assert out["status"] == "waiting"
        assert out["last_cycle"]["win"] is True

    def test_record_cycle_loss_increments_loss_count(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        out = store.record_pivot_loop_cycle(
            "AAPL", capital_at_start=1000.0,
            entry_price=305, entry_fill=305, entry_at="2026-06-03T14:00:00Z",
            shares=3, exit_fill=295, exit_at="2026-06-03T18:30:00Z",
            exit_reason="stop", realized_pnl=-30.0,
        )
        assert out["loss_count"] == 1
        assert out["win_count"] == 0
        assert out["current_capital"] == pytest.approx(970.0)
        assert out["last_cycle"]["win"] is False

    def test_record_cycle_no_compound_keeps_fixed_capital(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7, compound=False)
        out = store.record_pivot_loop_cycle(
            "AAPL", capital_at_start=1000.0,
            entry_price=305, entry_fill=305, entry_at="t",
            shares=3, exit_fill=315, exit_at="t",
            exit_reason="target", realized_pnl=30.0,
        )
        # Compounding OFF -> capital stays fixed
        assert out["current_capital"] == pytest.approx(1000.0)
        # But cumulative_realized still tracks lifetime P&L
        assert out["cumulative_realized"] == pytest.approx(30.0)

    def test_record_cycle_on_unknown_symbol_raises(self, store):
        with pytest.raises(ValueError, match="no pivot loop"):
            store.record_pivot_loop_cycle(
                "GME", capital_at_start=1000.0,
                entry_price=None, entry_fill=None, entry_at=None, shares=None,
                exit_fill=None, exit_at=None, exit_reason="manual",
                realized_pnl=0.0,
            )

    def test_get_cycles_returns_most_recent_first(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        for pnl in (10, 20, -5, 15):
            store.record_pivot_loop_cycle(
                "AAPL", capital_at_start=1000.0,
                entry_price=None, entry_fill=None, entry_at=None, shares=1,
                exit_fill=None, exit_at=None, exit_reason="manual",
                realized_pnl=float(pnl),
            )
        cycles = store.get_pivot_loop_cycles("AAPL")
        assert len(cycles) == 4
        # Most recent (cycle_number=4) first
        assert cycles[0]["cycle_number"] == 4
        assert cycles[0]["realized_pnl"] == 15.0
        assert cycles[-1]["cycle_number"] == 1

    def test_stop_cascade_preserves_cycles(self, store):
        store.create_pivot_loop("AAPL", 1000.0, 7)
        store.record_pivot_loop_cycle(
            "AAPL", capital_at_start=1000.0,
            entry_price=None, entry_fill=None, entry_at=None, shares=1,
            exit_fill=None, exit_at=None, exit_reason="manual",
            realized_pnl=10.0,
        )
        store.stop_pivot_loop("AAPL")
        # Row + cycles preserved (stop doesn't delete; just status flip)
        assert store.get_pivot_loop("AAPL") is not None
        assert len(store.get_pivot_loop_cycles("AAPL")) == 1


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
            (("GET", "HEAD"), "/chat/api/live/status"),
            (("POST",), "/chat/api/live/reset-breaker"),
            (("GET", "HEAD"), "/chat/api/pivot/{symbol}"),
            (("GET", "HEAD"), "/chat/api/loops"),
            (("POST",), "/chat/api/loops"),
            (("GET", "HEAD"), "/chat/api/loops/{symbol}"),
            (("PATCH",), "/chat/api/loops/{symbol}"),
            (("DELETE",), "/chat/api/loops/{symbol}"),
            (("POST",), "/chat/api/loops/{symbol}/cycles"),
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
