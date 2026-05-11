"""OCA (One-Cancels-All) group helpers.

Layer 1 supports `oca_type=1` only (cancel all remaining on any fill). Other
OCA types exist in IBKR but their semantics around partial fills are subtle
and not needed for the strategies in later layers.
"""

from __future__ import annotations

import uuid
from typing import Iterable, List

from .orders import OrderRequest, validate_request
from .utils import ValidationError


OCA_TYPE_CANCEL_ALL = 1


def make_group_id(prefix: str = "oca") -> str:
    """Stable, unique-per-group identifier shared by every order in the group."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def prepare_group(
    requests: Iterable[OrderRequest],
    group_name: str,
    oca_type: int = OCA_TYPE_CANCEL_ALL,
) -> List[OrderRequest]:
    """Validate every request, tag with shared OCA fields, and return them.

    Raises `ValidationError` and leaves no requests modified if any one fails.
    """
    requests = list(requests)
    if len(requests) < 2:
        raise ValidationError("OCA group requires at least 2 orders")
    if oca_type != OCA_TYPE_CANCEL_ALL:
        raise ValidationError(
            f"Only oca_type={OCA_TYPE_CANCEL_ALL} is supported in this layer"
        )
    if not group_name or not isinstance(group_name, str):
        raise ValidationError("oca_group_name must be a non-empty string")

    # Validate every request first so we never tag-then-fail.
    for i, req in enumerate(requests):
        try:
            validate_request(req)
        except ValidationError as e:
            raise ValidationError(f"order #{i + 1} invalid: {e}") from e

    for req in requests:
        req.oca_group = group_name
        req.oca_type = oca_type

    return requests
