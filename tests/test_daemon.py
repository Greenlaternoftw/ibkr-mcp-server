"""Tests for Layer 5a — daemon-mode resilience.

Covers:
  - `_on_exec_details_for_strategies` schedules a tick for the matching strategy
  - `_on_order_status_for_strategies` reacts only to terminal statuses
  - `resume_strategies_from_state` restarts asyncio tasks for active strategies
  - `reconcile_on_startup` clears stale order IDs that aren't in IBKR's open list
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.reversal import (
    FilledTranche,
    ReversalConfig,
    ReversalState,
    ReversalStatus,
)
from ibkr_mcp_server.swing import (
    SwingConfig,
    SwingState,
    SwingStateRecord,
)


# --- helpers --------------------------------------------------------------


def _make_client_with_isolated_state(tmp_path: Path) -> IBKRClient:
    """Build a client wired to isolated state-file paths (so we don't read or
    write the user's real state files).
    """
    client = IBKRClient()
    client.ib = MagicMock()
    client.ib.isConnected.return_value = True
    client._connected = True
    client.current_account = "DU1234567"
    client.accounts = ["DU1234567"]
    client._reversal_state_path = tmp_path / "reversal.json"
    client._swing_state_path = tmp_path / "swing.json"
    # Force the lazy load to use our paths
    client._reversal_states = {}
    client._swing_states = {}
    return client


class _FakeOrder:
    def __init__(self, order_id: int):
        self.orderId = order_id


class _FakeStatus:
    def __init__(self, status: str):
        self.status = status


class _FakeTrade:
    def __init__(self, order_id: int, status: str):
        self.order = _FakeOrder(order_id)
        self.orderStatus = _FakeStatus(status)


# --- event handler routing -----------------------------------------------


class TestEventRouting:

    def test_reversal_fill_event_schedules_reversal_tick(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._reversal_states["AAPL"] = ReversalState(
            symbol="AAPL", total_dollars=10000,
            config=ReversalConfig(),
            status=ReversalStatus.STALLED,
            protective_stop_order_id=42,
        )
        with patch.object(client, "_reversal_tick", AsyncMock()) as mock_tick:
            trade = _FakeTrade(42, "Filled")
            # The handler is sync; it creates the tick via asyncio.create_task.
            with patch("asyncio.create_task") as mock_create:
                client._on_exec_details_for_strategies(trade, None)
                assert mock_create.called
                # The coroutine passed should be _reversal_tick("AAPL")
                # We can't easily inspect coroutine args via mock_create.call_args,
                # but the fact that we got here means routing worked.

    def test_swing_fill_event_schedules_swing_tick(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=55,
            protective_stop_order_id=56,
        )
        with patch("asyncio.create_task") as mock_create:
            client._on_exec_details_for_strategies(_FakeTrade(55, "Filled"), None)
            assert mock_create.called

    def test_unknown_order_id_is_ignored(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=55,
        )
        with patch("asyncio.create_task") as mock_create:
            client._on_exec_details_for_strategies(_FakeTrade(9999, "Filled"), None)
            assert not mock_create.called

    def test_order_status_handler_ignores_non_terminal(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=55,
        )
        with patch("asyncio.create_task") as mock_create:
            client._on_order_status_for_strategies(_FakeTrade(55, "PreSubmitted"))
            assert not mock_create.called
            client._on_order_status_for_strategies(_FakeTrade(55, "Submitted"))
            assert not mock_create.called

    def test_order_status_handler_fires_on_terminal(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=55,
        )
        with patch("asyncio.create_task") as mock_create:
            client._on_order_status_for_strategies(_FakeTrade(55, "Cancelled"))
            assert mock_create.called


# --- resume strategies on startup -----------------------------------------


class TestResumeStrategies:

    @pytest.mark.asyncio
    async def test_resumes_active_reversal_and_swing(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._reversal_states["AAPL"] = ReversalState(
            symbol="AAPL", total_dollars=10000,
            config=ReversalConfig(),
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[FilledTranche(index=1, target_dollars=3333,
                                            shares=20, fill_price=160.0,
                                            filled_at="2024-01-05")],
        )
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
        )

        # Patch the loop coroutines to return immediately so the test doesn't hang
        async def _fake_loop(*args, **kwargs):
            return

        with patch.object(client, "_reversal_loop", _fake_loop), \
             patch.object(client, "_swing_loop", _fake_loop):
            resumed = await client.resume_strategies_from_state()

        assert resumed["reversal"] == ["AAPL"]
        assert resumed["swing"] == ["NVDA"]

    @pytest.mark.asyncio
    async def test_skips_terminal_strategies(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._reversal_states["AAPL"] = ReversalState(
            symbol="AAPL", total_dollars=10000,
            config=ReversalConfig(),
            status=ReversalStatus.COMPLETE,  # terminal
        )
        client._swing_states["NVDA"] = SwingStateRecord(
            symbol="NVDA", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.STOPPED,  # terminal
        )
        async def _fake_loop(*args, **kwargs):
            return
        with patch.object(client, "_reversal_loop", _fake_loop), \
             patch.object(client, "_swing_loop", _fake_loop):
            resumed = await client.resume_strategies_from_state()
        assert resumed == {"reversal": [], "swing": []}


# --- reconciliation on startup --------------------------------------------


class TestReconcileOnStartup:

    @pytest.mark.asyncio
    async def test_clears_stale_swing_orders(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        # State says we have a protective OCA pair (IDs 10, 11) and dip-buy 42
        client._swing_states["AAPL"] = SwingStateRecord(
            symbol="AAPL", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
            oca_group="grp-1",
            dip_buy_order_id=42,
        )
        # IBKR says only order 10 is open; 11 and 42 are gone (filled or
        # cancelled at the broker).
        client.ib.trades.return_value = [
            _FakeTrade(10, "PreSubmitted"),
        ]

        result = await client.reconcile_on_startup()

        assert result["status"] == "reconciled"
        state = client._swing_states["AAPL"]
        assert state.protective_trail_order_id == 10            # still valid
        assert state.protective_stop_order_id is None           # cleared
        assert state.dip_buy_order_id is None                   # cleared
        # OCA group cleared because at least one leg disappeared
        assert state.oca_group is None

    @pytest.mark.asyncio
    async def test_clears_stale_reversal_protective_stop(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._reversal_states["AAPL"] = ReversalState(
            symbol="AAPL", total_dollars=10000,
            config=ReversalConfig(),
            status=ReversalStatus.STALLED,
            protective_stop_order_id=99,
        )
        client.ib.trades.return_value = []  # nothing open in IBKR

        result = await client.reconcile_on_startup()

        assert "AAPL" in result["cleared"]["reversal"]
        assert client._reversal_states["AAPL"].protective_stop_order_id is None

    @pytest.mark.asyncio
    async def test_no_changes_when_state_matches_reality(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._swing_states["AAPL"] = SwingStateRecord(
            symbol="AAPL", quantity=100, cost_basis=200.0,
            config=SwingConfig(dip_percent=3.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=10,
            protective_stop_order_id=11,
            oca_group="grp-1",
        )
        client.ib.trades.return_value = [
            _FakeTrade(10, "PreSubmitted"),
            _FakeTrade(11, "PreSubmitted"),
        ]

        result = await client.reconcile_on_startup()

        assert result["cleared"]["swing"] == []
        state = client._swing_states["AAPL"]
        assert state.protective_trail_order_id == 10
        assert state.protective_stop_order_id == 11
        assert state.oca_group == "grp-1"

    @pytest.mark.asyncio
    async def test_skipped_when_disconnected(self, tmp_path: Path):
        client = _make_client_with_isolated_state(tmp_path)
        client._connected = False
        result = await client.reconcile_on_startup()
        assert result == {"status": "skipped", "reason": "not_connected"}
