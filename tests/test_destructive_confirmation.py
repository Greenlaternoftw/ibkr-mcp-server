"""Tests for the REQUIRE_CONFIRMATION_FOR_DESTRUCTIVE_TOOLS gate.

Covers the four destructive paths:
  - place_order (non-dry-run, live transmission)
  - place_oca_group (non-dry-run)
  - stop_swing_strategy (cancels live protective orders)
  - stop_reversal_entry (cancel / liquidate_filled)
  - update_swing_params (structural change cancels live orders)

Each test verifies:
  * gate OFF (default) → behavior unchanged
  * gate ON, confirm omitted/false → returns "needs_confirmation" preview;
    no IB calls; no state mutation
  * gate ON, confirm=true → executes normally
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.config import settings
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


@pytest.fixture(autouse=True)
def trading_enabled(monkeypatch):
    """Most tests need live-trading on to reach the destructive code paths.
    Individual tests can override via monkeypatch."""
    monkeypatch.setattr(settings, "enable_live_trading", True)
    monkeypatch.setattr(settings, "max_order_size", 1000)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(settings, "require_confirmation_for_destructive_tools", True)


@pytest.fixture
def client(tmp_path):
    c = IBKRClient()
    c.ib = MagicMock()
    c.ib.isConnected.return_value = True
    c._connected = True
    c.current_account = "DU1234567"
    c.accounts = ["DU1234567"]
    c._reversal_state_path = tmp_path / "reversal.json"
    c._swing_state_path = tmp_path / "swing.json"
    c._reversal_states = {}
    c._swing_states = {}

    async def _qualify(contract):
        contract.conId = 12345
        return [contract]

    c.ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

    placed = MagicMock()
    placed.order.orderId = 7777
    placed.order.permId = 88888888
    c.ib.placeOrder = MagicMock(return_value=placed)
    c.ib.trades = MagicMock(return_value=[])
    c.ib.cancelOrder = MagicMock()
    return c


# --- place_order ----------------------------------------------------------


class TestPlaceOrderGate:
    @pytest.mark.asyncio
    async def test_gate_off_no_confirm_executes(self, client):
        r = await client.place_order(symbol="AAPL", action="BUY", quantity=1, order_type="MKT")
        assert r["status"] == "submitted"

    @pytest.mark.asyncio
    async def test_gate_on_no_confirm_returns_needs_confirmation(self, client, gate_on):
        r = await client.place_order(symbol="AAPL", action="BUY", quantity=1, order_type="MKT")
        assert r["status"] == "needs_confirmation"
        assert r["action"] == "place_order"
        assert "confirm=true" in r["message"].lower()
        assert not client.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_gate_on_with_confirm_executes(self, client, gate_on):
        r = await client.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="MKT", confirm=True
        )
        assert r["status"] == "submitted"
        assert client.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_gate_on_dry_run_unaffected(self, client, gate_on):
        # dry_run is its own preview path — should NOT be re-gated.
        r = await client.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="MKT", dry_run=True
        )
        assert r["status"] == "dry_run"

    @pytest.mark.asyncio
    async def test_gate_on_validation_error_still_returns_error(self, client, gate_on):
        # Bad input should fail validation before reaching the gate.
        r = await client.place_order(symbol="AAPL", action="BUY", quantity=1, order_type="LMT")
        assert r["status"] == "error"
        assert "limit_price" in r["message"].lower()


# --- place_oca_group ------------------------------------------------------


class TestPlaceOcaGroupGate:
    _legs = [
        {"symbol": "AAPL", "action": "SELL", "quantity": 100,
         "order_type": "TRAIL", "trail_percent": 5.0},
        {"symbol": "AAPL", "action": "SELL", "quantity": 100,
         "order_type": "STP", "stop_price": 250.0},
    ]

    @pytest.mark.asyncio
    async def test_gate_off_executes(self, client):
        r = await client.place_oca_group(orders=self._legs, oca_group_name="g1")
        assert r["status"] == "submitted"

    @pytest.mark.asyncio
    async def test_gate_on_no_confirm_returns_needs_confirmation(self, client, gate_on):
        r = await client.place_oca_group(orders=self._legs, oca_group_name="g1")
        assert r["status"] == "needs_confirmation"
        assert r["preview"]["group_id"] == "g1"
        assert len(r["preview"]["legs"]) == 2
        assert not client.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_gate_on_with_confirm_executes(self, client, gate_on):
        r = await client.place_oca_group(orders=self._legs, oca_group_name="g1", confirm=True)
        assert r["status"] == "submitted"
        assert client.ib.placeOrder.call_count == 2

    @pytest.mark.asyncio
    async def test_gate_on_dry_run_unaffected(self, client, gate_on):
        r = await client.place_oca_group(orders=self._legs, oca_group_name="g1", dry_run=True)
        assert r["status"] == "dry_run"


# --- stop_swing_strategy --------------------------------------------------


class TestStopSwingGate:
    def _register(self, client, **overrides):
        client._swing_states["F"] = SwingStateRecord(
            symbol="F", quantity=10, cost_basis=13.36,
            config=SwingConfig(dip_percent=5.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=199,
            protective_stop_order_id=201,
            oca_group="grp-F",
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_gate_off_executes(self, client):
        self._register(client)
        r = await client.stop_swing_strategy("F")
        assert r["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_gate_on_no_confirm_returns_needs_confirmation(self, client, gate_on):
        self._register(client)
        r = await client.stop_swing_strategy("F")
        assert r["status"] == "needs_confirmation"
        assert r["action"] == "stop_swing_strategy"
        assert r["preview"]["symbol"] == "F"
        assert 199 in r["preview"]["orders_that_would_be_cancelled"]
        assert 201 in r["preview"]["orders_that_would_be_cancelled"]
        # State must NOT have changed
        assert client._swing_states["F"].state is SwingState.HOLDING
        assert client._swing_states["F"].protective_trail_order_id == 199

    @pytest.mark.asyncio
    async def test_gate_on_with_confirm_executes(self, client, gate_on):
        self._register(client)
        r = await client.stop_swing_strategy("F", confirm=True)
        assert r["status"] == "stopped"
        assert client._swing_states["F"].state is SwingState.STOPPED

    @pytest.mark.asyncio
    async def test_gate_on_missing_strategy_still_errors_immediately(self, client, gate_on):
        # The "no swing strategy for X" error should come back before the gate.
        r = await client.stop_swing_strategy("NOPE")
        assert r["status"] == "error"


# --- stop_reversal_entry --------------------------------------------------


class TestStopReversalGate:
    def _register(self, client, **overrides):
        client._reversal_states["TSLA"] = ReversalState(
            symbol="TSLA", total_dollars=30000,
            config=ReversalConfig(),
            status=ReversalStatus.PARTIALLY_FILLED,
            filled_tranches=[
                FilledTranche(index=1, target_dollars=10000, shares=20,
                              fill_price=500.0, filled_at="2024-01-05")
            ],
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_gate_off_executes(self, client):
        self._register(client)
        r = await client.stop_reversal_entry("TSLA", action="cancel")
        assert r["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_gate_on_cancel_returns_needs_confirmation(self, client, gate_on):
        self._register(client)
        r = await client.stop_reversal_entry("TSLA", action="cancel")
        assert r["status"] == "needs_confirmation"
        assert r["action"] == "stop_reversal_entry:cancel"
        assert r["preview"]["filled_tranches"] == 1
        assert r["preview"]["filled_shares"] == 20
        # State unchanged
        assert client._reversal_states["TSLA"].status is ReversalStatus.PARTIALLY_FILLED

    @pytest.mark.asyncio
    async def test_gate_on_liquidate_filled_describes_market_sell(self, client, gate_on):
        self._register(client)
        r = await client.stop_reversal_entry("TSLA", action="liquidate_filled")
        assert r["status"] == "needs_confirmation"
        assert "market-sell" in r["preview"]["would_do"].lower()

    @pytest.mark.asyncio
    async def test_gate_on_with_confirm_executes(self, client, gate_on):
        self._register(client)
        r = await client.stop_reversal_entry("TSLA", action="cancel", confirm=True)
        assert r["status"] == "cancelled"
        assert client._reversal_states["TSLA"].status is ReversalStatus.CANCELLED


# --- update_swing_params --------------------------------------------------


class TestUpdateSwingParamsGate:
    def _register(self, client):
        client._swing_states["F"] = SwingStateRecord(
            symbol="F", quantity=10, cost_basis=13.36,
            config=SwingConfig(dip_percent=5.0, floor_offset=1.0),
            state=SwingState.HOLDING,
            protective_trail_order_id=199,
            protective_stop_order_id=201,
            oca_group="grp-F",
        )

    @pytest.mark.asyncio
    async def test_gate_on_non_structural_change_unaffected(self, client, gate_on):
        # cooldown_hours is non-structural — shouldn't trigger the gate.
        self._register(client)
        r = await client.update_swing_params("F", cooldown_hours=12)
        assert r["status"] == "updated"
        assert client._swing_states["F"].config.cooldown_hours == 12

    @pytest.mark.asyncio
    async def test_gate_on_structural_change_returns_needs_confirmation(self, client, gate_on):
        self._register(client)
        r = await client.update_swing_params("F", floor_offset=2.0)
        assert r["status"] == "needs_confirmation"
        assert r["action"] == "update_swing_params:structural"
        assert r["preview"]["structural_changes"] == {"floor_offset": 2.0}
        assert 199 in r["preview"]["live_orders_that_would_be_cancelled"]
        # Config NOT applied yet
        assert client._swing_states["F"].config.floor_offset == 1.0

    @pytest.mark.asyncio
    async def test_gate_on_structural_with_confirm_executes(self, client, gate_on):
        self._register(client)
        r = await client.update_swing_params("F", floor_offset=2.0, confirm=True)
        assert r["status"] == "updated"
        assert client._swing_states["F"].config.floor_offset == 2.0
        assert r["structural_changed"] is True

    @pytest.mark.asyncio
    async def test_gate_off_structural_change_works(self, client):
        self._register(client)
        r = await client.update_swing_params("F", floor_offset=2.0)
        assert r["status"] == "updated"
        assert client._swing_states["F"].config.floor_offset == 2.0
