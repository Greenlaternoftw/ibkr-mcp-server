"""Tests for the architectural-fix safety properties.

These verify the three defences against the Bug #5/#6/#7 wedge class:
  1. Synchronous input validation rejects bad orders in <1ms (no IB call)
  2. asyncio.wait_for wrapping bounds IB calls to a known timeout
  3. Connection reset is scheduled on timeout so subsequent calls aren't stuck

All tests use mocked IB; no live Gateway needed.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.config import settings


@pytest.fixture(autouse=True)
def live_trading_enabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_live_trading", True)
    monkeypatch.setattr(settings, "max_order_size", 1000)


@pytest.fixture
def client_with_mock_ib():
    client = IBKRClient()
    client.ib = MagicMock()
    client.ib.isConnected.return_value = True
    client._connected = True
    client.current_account = "DU1234567"
    client.accounts = ["DU1234567"]

    async def _qualify(contract):
        contract.conId = 12345
        return [contract]

    client.ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

    placed = MagicMock()
    placed.order.orderId = 7777
    placed.order.permId = 88888888
    client.ib.placeOrder = MagicMock(return_value=placed)
    return client


# --- Validation fails fast (Bugs #5/#6 defence: validate before any IB call) ---


class TestValidationFailsFast:
    @pytest.mark.asyncio
    async def test_lmt_without_limit_price_returns_error_quickly(self, client_with_mock_ib):
        t0 = time.time()
        r = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="LMT"
        )
        elapsed = time.time() - t0
        assert r["status"] == "error"
        assert "limit_price" in r["message"].lower()
        assert elapsed < 0.1, f"Validation took {elapsed*1000:.1f}ms; expected <100ms"
        # Critical: no IB call was made for the invalid order
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_stp_without_stop_price_fails_fast(self, client_with_mock_ib):
        t0 = time.time()
        r = await client_with_mock_ib.place_order(
            symbol="AAPL", action="SELL", quantity=1, order_type="STP"
        )
        elapsed = time.time() - t0
        assert r["status"] == "error"
        assert "stop_price" in r["message"].lower()
        assert elapsed < 0.1
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_trail_with_no_amount_or_percent_fails_fast(self, client_with_mock_ib):
        t0 = time.time()
        r = await client_with_mock_ib.place_order(
            symbol="AAPL", action="SELL", quantity=1, order_type="TRAIL"
        )
        elapsed = time.time() - t0
        assert r["status"] == "error"
        assert "trail" in r["message"].lower()
        assert elapsed < 0.1
        assert not client_with_mock_ib.ib.placeOrder.called


# --- Timeouts bound IB calls + schedule reset (Bug #7 defence) ---


class TestTimeoutBounding:
    @pytest.mark.asyncio
    async def test_qualify_timeout_returns_error_quickly(self, client_with_mock_ib):
        """If qualifyContractsAsync hangs forever, place_order must return
        within ~QUALIFY_TIMEOUT seconds — NOT 4 minutes."""

        async def _hang_forever(_contract):
            await asyncio.sleep(60)  # would be 4-minute wedge without bounding
            return []

        client_with_mock_ib.ib.qualifyContractsAsync = AsyncMock(side_effect=_hang_forever)
        client_with_mock_ib.QUALIFY_TIMEOUT = 0.2  # shorter for the test
        # Stub the reset so we don't actually try to reconnect
        client_with_mock_ib._reset_on_timeout = AsyncMock(return_value=None)

        t0 = time.time()
        r = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="MKT"
        )
        elapsed = time.time() - t0

        assert r["status"] == "error"
        assert "timed out" in r["message"].lower()
        # Must return within timeout + small overhead
        assert elapsed < 1.0, f"place_order took {elapsed:.2f}s; expected <1s"
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_timeout_schedules_connection_reset(self, client_with_mock_ib):
        """Confirm the reset-on-timeout path is invoked."""

        async def _hang(_contract):
            await asyncio.sleep(60)
            return []

        client_with_mock_ib.ib.qualifyContractsAsync = AsyncMock(side_effect=_hang)
        client_with_mock_ib.QUALIFY_TIMEOUT = 0.2
        client_with_mock_ib._reset_on_timeout = AsyncMock(return_value=None)

        await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="MKT"
        )
        # Give the fire-and-forget create_task a moment to schedule.
        await asyncio.sleep(0.05)
        assert client_with_mock_ib._reset_on_timeout.called


# --- Subsequent reads work after a timeout (the real wedge defence) ---


class TestNoWedgeAfterTimeout:
    @pytest.mark.asyncio
    async def test_subsequent_calls_complete_after_bad_order_timeout(
        self, client_with_mock_ib
    ):
        """The pathological case: bad order causes timeout; subsequent read
        endpoints (like get_connection_status implicit via is_connected())
        must remain responsive. This proves the wedge bug is fixed."""

        async def _hang(_contract):
            await asyncio.sleep(60)
            return []

        client_with_mock_ib.ib.qualifyContractsAsync = AsyncMock(side_effect=_hang)
        client_with_mock_ib.QUALIFY_TIMEOUT = 0.2
        client_with_mock_ib._reset_on_timeout = AsyncMock(return_value=None)

        # Trigger the timeout
        await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=1, order_type="MKT"
        )

        # Now 10 read-side operations must complete fast.
        t0 = time.time()
        for _ in range(10):
            assert client_with_mock_ib.is_connected() is True
        elapsed = time.time() - t0
        assert elapsed < 0.5, f"10 reads took {elapsed:.2f}s; server appears wedged"


# --- Order lock serializes placements (no parallel order interleaving) ---


class TestOrderLock:
    @pytest.mark.asyncio
    async def test_two_concurrent_orders_serialize(self, client_with_mock_ib):
        """Two place_order calls should not interleave their qualify+place
        steps inside the lock. We prove serialization by tracking call order."""

        call_order: list = []

        async def _qualify(contract):
            call_order.append(f"qualify_start:{contract.symbol}")
            await asyncio.sleep(0.05)
            contract.conId = 12345
            call_order.append(f"qualify_end:{contract.symbol}")
            return [contract]

        client_with_mock_ib.ib.qualifyContractsAsync = AsyncMock(side_effect=_qualify)

        await asyncio.gather(
            client_with_mock_ib.place_order(symbol="AAPL", action="BUY", quantity=1, order_type="MKT"),
            client_with_mock_ib.place_order(symbol="NVDA", action="BUY", quantity=1, order_type="MKT"),
        )

        # Within the lock, qualify_end for one symbol must come before
        # qualify_start of the other (no interleaving).
        # Find the indices
        first_start = call_order[0]
        first_end_idx = call_order.index(first_start.replace("start", "end"))
        # The other symbol's start must be after the first's end
        second_start_idx = next(
            i for i, c in enumerate(call_order) if "qualify_start" in c and c != first_start
        )
        assert second_start_idx > first_end_idx, (
            f"Order placements interleaved — lock not serializing: {call_order}"
        )
