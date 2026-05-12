"""Manual paper-trade smoke test for Layer 1.

NOT a unit test — needs a real Gateway connection. Run from the VPS:

    .venv/bin/python tests/smoke_paper.py             # all scenarios dry_run
    .venv/bin/python tests/smoke_paper.py --live      # actually transmit

Requires `.env` with:
    IBKR_HOST=127.0.0.1
    IBKR_PORT=4002
    ENABLE_LIVE_TRADING=true
    MAX_ORDER_SIZE=1000     # high enough for the 50-100 share scenarios
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from ibkr_mcp_server.client import ibkr_client


DRY = "--live" not in sys.argv


def _intent(r: dict) -> str:
    """Pull the intent string, or fall back to the message if validation failed."""
    preview = r.get("preview") or {}
    return preview.get("intent") or r.get("message", "<no preview>")


def _print(label: str, r: dict, expected: str = "dry_run") -> None:
    ok = r["status"] == expected
    flag = "OK " if ok else "!! "
    print(f"{flag}{label}: status={r['status']}  -> {_intent(r)}")


async def main() -> None:
    print(f"=== Layer 1 paper smoke {'(DRY RUN)' if DRY else '(LIVE PAPER)'} ===")
    await ibkr_client.connect()
    expected = "dry_run" if DRY else "submitted"

    # S1: trailing SELL of 100 AAPL with $2 trail
    r = await ibkr_client.place_order(
        symbol="AAPL", action="SELL", quantity=100, order_type="TRAIL",
        trail_amount=2.0, dry_run=DRY,
    )
    _print("S1 trailing SELL AAPL $2 trail", r, expected)

    # S2: 3% trailing BUY of 50 NVDA
    r = await ibkr_client.place_order(
        symbol="NVDA", action="BUY", quantity=50, order_type="TRAIL",
        trail_percent=3.0, dry_run=DRY,
    )
    _print("S2 trailing BUY NVDA 3% trail", r, expected)

    # S3: OCA pair on AAPL — 5% trailing SELL + hard floor STP at $250
    r = await ibkr_client.place_oca_group(
        oca_group_name="aapl-protect-smoke",
        orders=[
            {"symbol": "AAPL", "action": "SELL", "quantity": 100,
             "order_type": "TRAIL", "trail_percent": 5.0, "dry_run": DRY},
            {"symbol": "AAPL", "action": "SELL", "quantity": 100,
             "order_type": "STP", "stop_price": 250.0, "dry_run": DRY},
        ],
    )
    flag = "OK " if r["status"] == expected else "!! "
    print(f"{flag}S3 OCA pair (TRAIL + STP): status={r['status']}  ({len(r['orders'])} legs)")
    for leg in r["orders"]:
        print(f"    - {leg['preview'].get('intent', '<no preview>')}")

    # S4 (validation gate): TRAIL with neither amount nor percent must be rejected
    r = await ibkr_client.place_order(
        symbol="AAPL", action="SELL", quantity=10, order_type="TRAIL",
        dry_run=DRY,
    )
    ok = r["status"] == "error" and "trail_amount" in r["message"]
    flag = "OK " if ok else "!! "
    print(f"{flag}S4 invalid TRAIL rejected: status={r['status']}  -> {r['message']}")

    # S5 (safety gate): quantity over MAX_ORDER_SIZE must be rejected
    # Use a large number that's higher than any reasonable cap
    r = await ibkr_client.place_order(
        symbol="AAPL", action="BUY", quantity=10_000_000, order_type="MKT",
        dry_run=DRY,
    )
    ok = r["status"] == "error" and "MAX_ORDER_SIZE" in r["message"]
    flag = "OK " if ok else "!! "
    print(f"{flag}S5 MAX_ORDER_SIZE cap enforced: status={r['status']}  -> {r['message']}")

    await ibkr_client.disconnect()
    print("=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
