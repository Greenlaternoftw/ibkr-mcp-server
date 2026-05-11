"""Order request validation and `ib_async.Order` construction.

This module is the single source of truth for order-type semantics. The
`IBKRClient.place_order` method and the `place_oca_group` helper both feed
their inputs through `validate_request` and `build_order` here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ib_async import Order

from .config import settings
from .utils import ValidationError, validate_symbol


SIMPLE_ORDER_TYPES = {"MKT", "LMT", "STP", "STP LMT"}
EXTENDED_ORDER_TYPES = {"TRAIL", "TRAIL LIMIT", "LOO", "MOO", "LOC", "MOC"}
VALID_ORDER_TYPES = SIMPLE_ORDER_TYPES | EXTENDED_ORDER_TYPES

VALID_ACTIONS = {"BUY", "SELL"}
VALID_TIFS = {"DAY", "GTC", "IOC", "OPG"}


@dataclass
class OrderRequest:
    """Normalized order request. Callers can pass kwargs to `from_kwargs`."""

    symbol: str
    action: str
    quantity: int
    order_type: str
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    trail_stop_price: Optional[float] = None
    limit_price_offset: Optional[float] = None
    tif: Optional[str] = None
    outside_rth: bool = False
    account: Optional[str] = None
    dry_run: bool = False
    oca_group: Optional[str] = None
    oca_type: Optional[int] = None

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> "OrderRequest":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(kwargs) - allowed
        if unknown:
            raise ValidationError(f"Unknown order parameter(s): {sorted(unknown)}")
        return cls(**{k: v for k, v in kwargs.items() if k in allowed})


def validate_request(req: OrderRequest) -> None:
    """Raise `ValidationError` if the request is malformed.

    Also normalizes a couple of fields in-place (uppercases action / order_type / tif,
    cleans the symbol). All cross-field rules from Layer 1 live here.
    """
    req.symbol = validate_symbol(req.symbol)

    if not isinstance(req.action, str):
        raise ValidationError("action must be a string")
    req.action = req.action.strip().upper()
    if req.action not in VALID_ACTIONS:
        raise ValidationError(f"action must be one of {sorted(VALID_ACTIONS)}")

    if not isinstance(req.quantity, int) or isinstance(req.quantity, bool):
        raise ValidationError("quantity must be a positive integer")
    if req.quantity <= 0:
        raise ValidationError("quantity must be > 0")
    if req.quantity > settings.max_order_size:
        raise ValidationError(
            f"quantity {req.quantity} exceeds MAX_ORDER_SIZE ({settings.max_order_size})"
        )

    if not isinstance(req.order_type, str):
        raise ValidationError("order_type must be a string")
    req.order_type = req.order_type.strip().upper()
    if req.order_type not in VALID_ORDER_TYPES:
        raise ValidationError(
            f"order_type must be one of {sorted(VALID_ORDER_TYPES)}"
        )

    if req.tif is not None:
        req.tif = req.tif.strip().upper()
        if req.tif not in VALID_TIFS:
            raise ValidationError(f"tif must be one of {sorted(VALID_TIFS)}")

    # Per-order-type rules
    ot = req.order_type
    if ot == "MKT":
        _reject_price_fields(req, "MKT")
    elif ot == "LMT":
        _require(req.limit_price is not None, "LMT requires limit_price")
        _reject(req.stop_price is not None, "LMT must not have stop_price")
    elif ot == "STP":
        _require(req.stop_price is not None, "STP requires stop_price")
        _reject(req.limit_price is not None, "STP must not have limit_price")
    elif ot == "STP LMT":
        _require(req.stop_price is not None, "STP LMT requires stop_price")
        _require(req.limit_price is not None, "STP LMT requires limit_price")
    elif ot in ("TRAIL", "TRAIL LIMIT"):
        has_amount = req.trail_amount is not None
        has_percent = req.trail_percent is not None
        if has_amount == has_percent:
            raise ValidationError(
                f"{ot} requires exactly one of trail_amount or trail_percent"
            )
        if has_percent and not (0 < req.trail_percent < 100):
            raise ValidationError("trail_percent must be in (0, 100)")
        if ot == "TRAIL LIMIT":
            _require(
                req.limit_price_offset is not None,
                "TRAIL LIMIT requires limit_price_offset",
            )
    elif ot in ("LOO", "LOC"):
        _require(req.limit_price is not None, f"{ot} requires limit_price")
        _reject(req.stop_price is not None, f"{ot} must not have stop_price")
    elif ot in ("MOO", "MOC"):
        _reject(req.limit_price is not None, f"{ot} must not have limit_price")
        _reject(req.stop_price is not None, f"{ot} must not have stop_price")

    # Auto-set TIF for open/close session orders
    if ot in ("LOO", "MOO"):
        req.tif = "OPG"
    elif ot in ("LOC", "MOC"):
        req.tif = "DAY"


def build_order(req: OrderRequest) -> Order:
    """Convert a validated `OrderRequest` into an `ib_async.Order`."""
    order = Order()
    order.action = req.action
    order.totalQuantity = req.quantity
    order.outsideRth = req.outside_rth
    if req.account:
        order.account = req.account
    if req.tif:
        order.tif = req.tif
    if req.oca_group:
        order.ocaGroup = req.oca_group
        if req.oca_type is not None:
            order.ocaType = req.oca_type

    ot = req.order_type

    if ot == "MKT":
        order.orderType = "MKT"
    elif ot == "LMT":
        order.orderType = "LMT"
        order.lmtPrice = req.limit_price
    elif ot == "STP":
        order.orderType = "STP"
        order.auxPrice = req.stop_price
    elif ot == "STP LMT":
        order.orderType = "STP LMT"
        order.auxPrice = req.stop_price
        order.lmtPrice = req.limit_price
    elif ot in ("TRAIL", "TRAIL LIMIT"):
        order.orderType = ot
        if req.trail_amount is not None:
            order.auxPrice = req.trail_amount
        if req.trail_percent is not None:
            order.trailingPercent = req.trail_percent
        if req.trail_stop_price is not None:
            order.trailStopPrice = req.trail_stop_price
        if ot == "TRAIL LIMIT":
            order.lmtPriceOffset = req.limit_price_offset
    elif ot == "LOO":
        order.orderType = "LMT"
        order.lmtPrice = req.limit_price
    elif ot == "MOO":
        order.orderType = "MKT"
    elif ot == "LOC":
        order.orderType = "LOC"
        order.lmtPrice = req.limit_price
    elif ot == "MOC":
        order.orderType = "MOC"

    return order


def describe_intent(req: OrderRequest) -> str:
    """Plain-English description of what the order is trying to do."""
    qty = req.quantity
    sym = req.symbol
    act = req.action
    ot = req.order_type

    if ot == "MKT":
        return f"Market order: {act} {qty} {sym} at the current market price"
    if ot == "LMT":
        return f"Limit order: {act} {qty} {sym} at ${req.limit_price} or better"
    if ot == "STP":
        return (
            f"Stop order: trigger a market {act} of {qty} {sym} when "
            f"price crosses ${req.stop_price}"
        )
    if ot == "STP LMT":
        return (
            f"Stop-limit order: when price crosses ${req.stop_price}, "
            f"{act} {qty} {sym} at ${req.limit_price} or better"
        )
    if ot in ("TRAIL", "TRAIL LIMIT"):
        trail = (
            f"${req.trail_amount}"
            if req.trail_amount is not None
            else f"{req.trail_percent}%"
        )
        if act == "BUY":
            base = f"Buy-the-dip: BUY {qty} {sym} when price rises by {trail} from its lowest point"
        else:
            base = f"Trailing stop loss: SELL {qty} {sym} when price falls by {trail} from its highest point"
        if ot == "TRAIL LIMIT":
            base += f", filled at trigger ± ${req.limit_price_offset}"
        return base
    if ot == "LOO":
        return f"Limit on Open: {act} {qty} {sym} at ${req.limit_price} or better, at market open"
    if ot == "MOO":
        return f"Market on Open: {act} {qty} {sym} at market open"
    if ot == "LOC":
        return f"Limit on Close: {act} {qty} {sym} at ${req.limit_price} or better, at market close"
    if ot == "MOC":
        return f"Market on Close: {act} {qty} {sym} at market close"
    return f"{ot} order: {act} {qty} {sym}"


def make_preview(req: OrderRequest) -> dict:
    """Echo all order fields plus the intent string."""
    return {
        "symbol": req.symbol,
        "action": req.action,
        "quantity": req.quantity,
        "order_type": req.order_type,
        "limit_price": req.limit_price,
        "stop_price": req.stop_price,
        "trail_amount": req.trail_amount,
        "trail_percent": req.trail_percent,
        "trail_stop_price": req.trail_stop_price,
        "limit_price_offset": req.limit_price_offset,
        "tif": req.tif,
        "outside_rth": req.outside_rth,
        "account": req.account,
        "oca_group": req.oca_group,
        "oca_type": req.oca_type,
        "intent": describe_intent(req),
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _reject(condition: bool, message: str) -> None:
    if condition:
        raise ValidationError(message)


def _reject_price_fields(req: OrderRequest, label: str) -> None:
    if req.limit_price is not None:
        raise ValidationError(f"{label} must not have limit_price")
    if req.stop_price is not None:
        raise ValidationError(f"{label} must not have stop_price")
