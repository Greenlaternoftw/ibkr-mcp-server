"""Tests for the portfolio equity-curve infrastructure.

Three layers under test:

  * ChatStore.{record_snapshot, get_snapshots, snapshot_count} -- the
    SQLite-backed history that the chart reads from.
  * IBKRClient.record_portfolio_snapshot -- calls get_account_summary,
    extracts NetLiquidation, writes one row.
  * IBKRClient.get_portfolio_equity_curve -- chart renderer + error
    path when there's not enough data.

Matplotlib + the network IBKR call are mocked. The persistence layer
runs against a real (tmp_path) SQLite file so the round-trip is real.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ibkr_mcp_server.chat.persistence import ChatStore
from ibkr_mcp_server.client import IBKRClient


_TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000A49444154789C6300010000000500010D0A2DB40000000049454E44AE426082"
)


# --- ChatStore snapshot CRUD ----------------------------------------------


class TestSnapshotStorage:
    def test_empty_returns_no_rows(self, tmp_path):
        store = ChatStore(tmp_path / "chat.db")
        assert store.get_snapshots(account="DU1") == []
        assert store.snapshot_count() == 0

    def test_record_then_get_round_trip(self, tmp_path):
        store = ChatStore(tmp_path / "chat.db")
        store.record_snapshot(
            account="DU1", net_liquidation=100_000.0,
            total_cash=20_000.0, positions_value=80_000.0,
            buying_power=200_000.0,
        )
        rows = store.get_snapshots(account="DU1")
        assert len(rows) == 1
        assert rows[0]["net_liquidation"] == 100_000.0
        assert rows[0]["total_cash"] == 20_000.0
        assert rows[0]["positions_value"] == 80_000.0
        assert rows[0]["buying_power"] == 200_000.0

    def test_snapshots_isolated_per_account(self, tmp_path):
        """Multi-account support: pulling DU1's curve shouldn't include
        DU2's data."""
        store = ChatStore(tmp_path / "chat.db")
        store.record_snapshot(account="DU1", net_liquidation=100.0)
        store.record_snapshot(account="DU2", net_liquidation=500.0)
        assert len(store.get_snapshots(account="DU1")) == 1
        assert store.get_snapshots(account="DU1")[0]["net_liquidation"] == 100.0
        assert store.get_snapshots(account="DU2")[0]["net_liquidation"] == 500.0

    def test_snapshots_returned_in_chronological_order(self, tmp_path):
        store = ChatStore(tmp_path / "chat.db")
        # millisecond precision in _utc_now_iso ensures these sort correctly
        for v in (100.0, 110.0, 105.0):
            store.record_snapshot(account="DU1", net_liquidation=v)
        rows = store.get_snapshots(account="DU1")
        assert [r["net_liquidation"] for r in rows] == [100.0, 110.0, 105.0]

    def test_count_total_and_per_account(self, tmp_path):
        store = ChatStore(tmp_path / "chat.db")
        store.record_snapshot(account="DU1", net_liquidation=100.0)
        store.record_snapshot(account="DU1", net_liquidation=110.0)
        store.record_snapshot(account="DU2", net_liquidation=500.0)
        assert store.snapshot_count() == 3
        assert store.snapshot_count(account="DU1") == 2
        assert store.snapshot_count(account="DU2") == 1


# --- IBKRClient.record_portfolio_snapshot --------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Bare client wired so the snapshot writes to a temp chat.db."""
    from ibkr_mcp_server.config import settings as _settings
    monkeypatch.setattr(_settings, "chat_db_path", str(tmp_path / "chat.db"))
    c = IBKRClient()
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c._connected = True
    c.current_account = "DU1"
    return c


class TestRecordSnapshot:
    @pytest.mark.asyncio
    async def test_writes_row_with_account_summary_values(self, client, tmp_path):
        # Mock get_account_summary to return realistic values.
        with patch.object(
            client, "get_account_summary",
            new=AsyncMock(return_value={
                "account": "DU1",
                "NetLiquidation": "100000.50",
                "TotalCashValue": "20000.00",
                "BuyingPower": "180000.00",
            }),
        ):
            result = await client.record_portfolio_snapshot()

        assert result["status"] == "ok"
        assert result["account"] == "DU1"
        assert result["net_liquidation"] == 100000.50
        assert result["positions_value"] == pytest.approx(80000.50)  # net - cash

        # Verify it landed in the store.
        store = ChatStore(tmp_path / "chat.db")
        rows = store.get_snapshots(account="DU1")
        assert len(rows) == 1
        assert rows[0]["net_liquidation"] == 100000.50

    @pytest.mark.asyncio
    async def test_missing_netliq_returns_error(self, client):
        """If account summary has no NetLiquidation, we don't write a
        garbage row -- we surface the error so the snapshot loop logs
        it as a warning."""
        with patch.object(
            client, "get_account_summary",
            new=AsyncMock(return_value={"account": "DU1"}),  # no NetLiq
        ):
            result = await client.record_portfolio_snapshot()
        assert result["status"] == "error"
        assert "NetLiquidation" in result["message"]


# --- IBKRClient.get_portfolio_equity_curve -------------------------------


class TestEquityCurveTool:
    @pytest.mark.asyncio
    async def test_returns_error_when_too_few_snapshots(self, client, tmp_path):
        """The error message has to tell the user what to do (wait) --
        the chat agent surfaces it to the operator."""
        store = ChatStore(tmp_path / "chat.db")
        store.record_snapshot(account="DU1", net_liquidation=100.0)  # only 1
        out = await client.get_portfolio_equity_curve()
        assert out["status"] == "error"
        assert out["snapshots_available"] == 1
        assert "snapshot" in out["message"].lower()
        assert "image_png_b64" not in out

    @pytest.mark.asyncio
    async def test_renders_chart_with_sufficient_snapshots(self, client, tmp_path):
        store = ChatStore(tmp_path / "chat.db")
        for v in (100.0, 105.0, 110.0):
            store.record_snapshot(account="DU1", net_liquidation=v)

        with patch(
            "ibkr_mcp_server.charts.render_equity_curve", return_value=_TINY_PNG,
        ):
            out = await client.get_portfolio_equity_curve()

        assert out["status"] == "ok"
        assert out["snapshots_in_window"] == 3
        assert out["first_value"] == 100.0
        assert out["last_value"] == 110.0
        assert out["pct_change"] == 10.0
        assert "image_png_b64" in out

    @pytest.mark.asyncio
    async def test_render_runs_on_thread(self, client, tmp_path):
        """Same event-loop-protection rule as the other chart tools."""
        store = ChatStore(tmp_path / "chat.db")
        for v in (100.0, 110.0):
            store.record_snapshot(account="DU1", net_liquidation=v)

        with patch(
            "asyncio.to_thread", new=AsyncMock(return_value=_TINY_PNG),
        ) as to_thread:
            out = await client.get_portfolio_equity_curve()
            assert to_thread.await_count == 1
            assert to_thread.await_args.args[0].__name__ == "render_equity_curve"
        assert out["status"] == "ok"

    @pytest.mark.asyncio
    async def test_respects_lookback_days_filter(self, client, tmp_path):
        """Old snapshots outside the lookback window should be excluded
        from the chart. Verified by inserting one OLD row directly (with
        a manually-set timestamp) and confirming get_snapshots filters
        it out under a tight lookback."""
        store = ChatStore(tmp_path / "chat.db")
        # Manually insert a 60-day-old row, then record a fresh one.
        import datetime as _dt
        old_ts = (
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=60)
        ).isoformat(timespec="milliseconds")
        with store._conn() as c:
            c.execute(
                "INSERT INTO portfolio_snapshots(timestamp, account, net_liquidation) "
                "VALUES (?, ?, ?)",
                (old_ts, "DU1", 50.0),
            )
        store.record_snapshot(account="DU1", net_liquidation=120.0)

        # Within 7-day window, only the fresh one is visible -- which
        # means insufficient data -> error path.
        with patch(
            "ibkr_mcp_server.charts.render_equity_curve", return_value=_TINY_PNG,
        ):
            out = await client.get_portfolio_equity_curve(lookback_days=7)
        assert out["status"] == "error"
        assert out["snapshots_available"] == 1

        # With a wider window both are visible.
        with patch(
            "ibkr_mcp_server.charts.render_equity_curve", return_value=_TINY_PNG,
        ):
            out = await client.get_portfolio_equity_curve(lookback_days=90)
        assert out["status"] == "ok"
        assert out["snapshots_in_window"] == 2
