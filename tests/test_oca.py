"""Tests for OCA group validation and the `place_oca_group` method."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.config import settings
from ibkr_mcp_server.oca import make_group_id, prepare_group
from ibkr_mcp_server.orders import OrderRequest
from ibkr_mcp_server.utils import ValidationError


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

    order_id_counter = [1000]

    def _place(_contract, order):
        trade = MagicMock()
        order_id_counter[0] += 1
        trade.order.orderId = order_id_counter[0]
        trade.order.permId = order_id_counter[0] * 10
        return trade

    client.ib.placeOrder = MagicMock(side_effect=_place)
    return client


def _protective_pair() -> list[dict]:
    """Trailing-stop sell + hard-floor stop on the same position."""
    return [
        dict(symbol="AAPL", action="SELL", quantity=100, order_type="TRAIL", trail_percent=5.0),
        dict(symbol="AAPL", action="SELL", quantity=100, order_type="STP", stop_price=250.0),
    ]


class TestPrepareGroup:
    def test_shares_group_string_and_type(self):
        reqs = [OrderRequest.from_kwargs(**o) for o in _protective_pair()]
        prepared = prepare_group(reqs, group_name="grp-1")
        assert all(r.oca_group == "grp-1" for r in prepared)
        assert all(r.oca_type == 1 for r in prepared)

    def test_rejects_single_order(self):
        with pytest.raises(ValidationError, match="at least 2"):
            prepare_group(
                [OrderRequest.from_kwargs(**_protective_pair()[0])], group_name="grp-1"
            )

    def test_rejects_unsupported_oca_type(self):
        reqs = [OrderRequest.from_kwargs(**o) for o in _protective_pair()]
        with pytest.raises(ValidationError, match="oca_type"):
            prepare_group(reqs, group_name="grp-1", oca_type=2)

    def test_invalid_leg_rejects_whole_group(self):
        bad = _protective_pair()
        bad[1]["order_type"] = "LMT"  # LMT without limit_price → invalid
        bad[1].pop("stop_price", None)
        reqs = [OrderRequest.from_kwargs(**o) for o in bad]
        with pytest.raises(ValidationError, match="order #2"):
            prepare_group(reqs, group_name="grp-1")
        # The first request must not have been tagged with the group.
        assert reqs[0].oca_group is None

    def test_make_group_id_unique(self):
        assert make_group_id() != make_group_id()
        assert make_group_id("trade").startswith("trade-")


class TestPlaceOcaGroup:
    @pytest.mark.asyncio
    async def test_submit_pair(self, client_with_mock_ib):
        result = await client_with_mock_ib.place_oca_group(
            orders=_protective_pair(), oca_group_name="grp-1"
        )
        assert result["status"] == "submitted"
        assert len(result["orders"]) == 2
        assert client_with_mock_ib.ib.placeOrder.call_count == 2

        # Both ib_async.Order objects share the OCA group.
        sent_orders = [call.args[1] for call in client_with_mock_ib.ib.placeOrder.call_args_list]
        assert all(o.ocaGroup == "grp-1" for o in sent_orders)
        assert all(o.ocaType == 1 for o in sent_orders)

    @pytest.mark.asyncio
    async def test_invalid_leg_blocks_all_transmission(self, client_with_mock_ib):
        bad = _protective_pair()
        bad[1] = dict(symbol="AAPL", action="SELL", quantity=100, order_type="LMT")  # no limit_price
        result = await client_with_mock_ib.place_oca_group(orders=bad, oca_group_name="grp-1")
        assert result["status"] == "error"
        assert client_with_mock_ib.ib.placeOrder.call_count == 0

    @pytest.mark.asyncio
    async def test_live_trading_disabled_blocks(self, client_with_mock_ib, monkeypatch):
        monkeypatch.setattr(settings, "enable_live_trading", False)
        result = await client_with_mock_ib.place_oca_group(
            orders=_protective_pair(), oca_group_name="grp-1"
        )
        assert result["status"] == "blocked"
        assert client_with_mock_ib.ib.placeOrder.call_count == 0

    @pytest.mark.asyncio
    async def test_dry_run_blocks_transmission(self, client_with_mock_ib):
        orders = _protective_pair()
        orders[0]["dry_run"] = True
        result = await client_with_mock_ib.place_oca_group(
            orders=orders, oca_group_name="grp-1"
        )
        assert result["status"] == "dry_run"
        assert client_with_mock_ib.ib.placeOrder.call_count == 0

    @pytest.mark.asyncio
    async def test_fill_on_one_cancels_the_other(self, client_with_mock_ib):
        """Mock check: orders share the OCA group, so IBKR's broker-side rule cancels.

        We can't fully simulate IBKR's broker-side OCA cancellation in a unit test,
        but we *can* prove that both `ib_async.Order` objects carry the same
        `ocaGroup`/`ocaType`, which is the necessary precondition for IBKR to do the
        cancellation. Live verification belongs in the paper-trading acceptance
        tests for this layer.
        """
        result = await client_with_mock_ib.place_oca_group(
            orders=_protective_pair(), oca_group_name="grp-1"
        )
        assert result["status"] == "submitted"
        sent_orders = [call.args[1] for call in client_with_mock_ib.ib.placeOrder.call_args_list]
        assert len({o.ocaGroup for o in sent_orders}) == 1
        assert all(o.ocaType == 1 for o in sent_orders)
