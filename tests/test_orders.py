"""Tests for order validation, building, and the `place_order` method."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ib_async import Order

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.config import settings
from ibkr_mcp_server.orders import (
    OrderRequest,
    build_order,
    describe_intent,
    make_preview,
    validate_request,
)
from ibkr_mcp_server.utils import ValidationError


# --- helpers ---------------------------------------------------------------


def _req(**overrides) -> OrderRequest:
    base = dict(symbol="AAPL", action="BUY", quantity=10, order_type="MKT")
    base.update(overrides)
    return OrderRequest.from_kwargs(**base)


@pytest.fixture(autouse=True)
def live_trading_enabled(monkeypatch):
    """All tests assume the safety gate is open unless they explicitly close it."""
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

    placed_trade = MagicMock()
    placed_trade.order.orderId = 7777
    placed_trade.order.permId = 88888888
    client.ib.placeOrder = MagicMock(return_value=placed_trade)
    return client


# --- order building --------------------------------------------------------


class TestOrderBuilding:
    def test_market_order(self):
        req = _req(order_type="MKT")
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "MKT"
        assert order.action == "BUY"
        assert order.totalQuantity == 10

    def test_limit_order(self):
        req = _req(order_type="LMT", limit_price=150.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "LMT"
        assert order.lmtPrice == 150.0

    def test_stop_order(self):
        req = _req(action="SELL", order_type="STP", stop_price=140.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "STP"
        assert order.auxPrice == 140.0

    def test_stop_limit_order(self):
        req = _req(action="SELL", order_type="STP LMT", stop_price=140.0, limit_price=139.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "STP LMT"
        assert order.auxPrice == 140.0
        assert order.lmtPrice == 139.0

    def test_trail_amount(self):
        req = _req(action="SELL", order_type="TRAIL", trail_amount=2.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "TRAIL"
        assert order.auxPrice == 2.0

    def test_trail_percent(self):
        req = _req(action="SELL", order_type="TRAIL", trail_percent=3.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "TRAIL"
        assert order.trailingPercent == 3.0

    def test_trail_limit(self):
        req = _req(
            action="SELL",
            order_type="TRAIL LIMIT",
            trail_percent=3.0,
            limit_price_offset=0.05,
        )
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "TRAIL LIMIT"
        assert order.trailingPercent == 3.0
        assert order.lmtPriceOffset == 0.05

    def test_loo_autosets_tif_opg(self):
        req = _req(order_type="LOO", limit_price=150.0)
        validate_request(req)
        assert req.tif == "OPG"
        order = build_order(req)
        assert order.orderType == "LMT"
        assert order.tif == "OPG"
        assert order.lmtPrice == 150.0

    def test_moo_autosets_tif_opg(self):
        req = _req(order_type="MOO")
        validate_request(req)
        assert req.tif == "OPG"
        order = build_order(req)
        assert order.orderType == "MKT"
        assert order.tif == "OPG"

    def test_loc_uses_loc_order_type(self):
        req = _req(order_type="LOC", limit_price=150.0)
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "LOC"
        assert order.lmtPrice == 150.0

    def test_moc_uses_moc_order_type(self):
        req = _req(order_type="MOC")
        validate_request(req)
        order = build_order(req)
        assert order.orderType == "MOC"


# --- validation rules ------------------------------------------------------


class TestValidation:
    def test_lmt_without_limit_price(self):
        with pytest.raises(ValidationError, match="LMT requires limit_price"):
            validate_request(_req(order_type="LMT"))

    def test_stp_without_stop_price(self):
        with pytest.raises(ValidationError, match="STP requires stop_price"):
            validate_request(_req(order_type="STP"))

    def test_trail_with_both_amount_and_percent(self):
        with pytest.raises(ValidationError, match="exactly one"):
            validate_request(_req(order_type="TRAIL", trail_amount=2.0, trail_percent=3.0))

    def test_trail_with_neither(self):
        with pytest.raises(ValidationError, match="exactly one"):
            validate_request(_req(order_type="TRAIL"))

    def test_trail_limit_without_offset(self):
        with pytest.raises(ValidationError, match="limit_price_offset"):
            validate_request(_req(order_type="TRAIL LIMIT", trail_percent=3.0))

    def test_trail_percent_out_of_range(self):
        with pytest.raises(ValidationError, match="trail_percent"):
            validate_request(_req(order_type="TRAIL", trail_percent=150.0))

    def test_loo_without_limit_price(self):
        with pytest.raises(ValidationError, match="LOO requires limit_price"):
            validate_request(_req(order_type="LOO"))

    def test_moo_with_limit_price(self):
        with pytest.raises(ValidationError, match="MOO must not have limit_price"):
            validate_request(_req(order_type="MOO", limit_price=150.0))

    def test_mkt_with_limit_price(self):
        with pytest.raises(ValidationError, match="MKT must not have limit_price"):
            validate_request(_req(order_type="MKT", limit_price=150.0))

    def test_quantity_exceeds_max(self, monkeypatch):
        monkeypatch.setattr(settings, "max_order_size", 100)
        with pytest.raises(ValidationError, match="MAX_ORDER_SIZE"):
            validate_request(_req(quantity=101))

    def test_quantity_zero(self):
        with pytest.raises(ValidationError, match="> 0"):
            validate_request(_req(quantity=0))

    def test_bad_action(self):
        with pytest.raises(ValidationError, match="action must be one of"):
            validate_request(_req(action="HOLD"))

    def test_bad_order_type(self):
        with pytest.raises(ValidationError, match="order_type must be one of"):
            validate_request(_req(order_type="WHATEVER"))

    def test_unknown_kwarg(self):
        with pytest.raises(ValidationError, match="Unknown order parameter"):
            OrderRequest.from_kwargs(
                symbol="AAPL", action="BUY", quantity=1, order_type="MKT", banana=True
            )


# --- intent descriptions ---------------------------------------------------


class TestIntent:
    def test_trail_buy(self):
        req = _req(order_type="TRAIL", trail_amount=2.0)
        validate_request(req)
        assert "Buy-the-dip" in describe_intent(req)

    def test_trail_sell(self):
        req = _req(action="SELL", order_type="TRAIL", trail_amount=2.0)
        validate_request(req)
        assert "Trailing stop loss" in describe_intent(req)

    def test_market(self):
        req = _req(order_type="MKT")
        validate_request(req)
        assert "Market order" in describe_intent(req)

    def test_preview_includes_intent(self):
        req = _req(order_type="LMT", limit_price=150.0)
        validate_request(req)
        preview = make_preview(req)
        assert "intent" in preview
        assert preview["limit_price"] == 150.0


# --- place_order method ----------------------------------------------------


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_submit_market_order(self, client_with_mock_ib):
        result = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=10, order_type="MKT"
        )
        assert result["status"] == "submitted"
        assert result["order_id"] == 7777
        assert result["perm_id"] == 88888888
        assert client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_dry_run_does_not_transmit(self, client_with_mock_ib):
        result = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=10, order_type="MKT", dry_run=True
        )
        assert result["status"] == "dry_run"
        assert result["preview"]["intent"]
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_live_trading_disabled_blocks(self, client_with_mock_ib, monkeypatch):
        monkeypatch.setattr(settings, "enable_live_trading", False)
        result = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=10, order_type="MKT"
        )
        assert result["status"] == "blocked"
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_validation_error_returns_error_status(self, client_with_mock_ib):
        result = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=10, order_type="LMT"
        )
        assert result["status"] == "error"
        assert "limit_price" in result["message"]
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_max_order_size_rejects(self, client_with_mock_ib, monkeypatch):
        monkeypatch.setattr(settings, "max_order_size", 5)
        result = await client_with_mock_ib.place_order(
            symbol="AAPL", action="BUY", quantity=100, order_type="MKT"
        )
        assert result["status"] == "error"
        assert "MAX_ORDER_SIZE" in result["message"]
        assert not client_with_mock_ib.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_trailing_sell_full_path(self, client_with_mock_ib):
        result = await client_with_mock_ib.place_order(
            symbol="AAPL",
            action="SELL",
            quantity=100,
            order_type="TRAIL",
            trail_amount=2.0,
        )
        assert result["status"] == "submitted"
        sent_order = client_with_mock_ib.ib.placeOrder.call_args.args[1]
        assert isinstance(sent_order, Order)
        assert sent_order.orderType == "TRAIL"
        assert sent_order.auxPrice == 2.0
