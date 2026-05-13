"""Smoke test for the architectural fix (Phase 1 of the fix doc).

Verifies the five assertions from CLAUDE_CODE_INSTRUCTIONS.md:

  1. Malformed LMT fails fast (<1s) with a structured error
  2. Server still responds after Test 1
  3. Valid dry_run order works
  4. (Skipped — OCA dry_run is now group-level, see test_safety.py)
  5. After a bad order, 10 sequential reads complete quickly (no wedge)

Run on the VPS (or anywhere the daemon is reachable):

    .venv/bin/python scripts/smoke_test_fixes.py

Uses a separate clientId so it doesn't fight with the systemd daemon.
"""

from __future__ import annotations

import asyncio
import time

from ibkr_mcp_server.client import ibkr_client


async def main() -> None:
    ibkr_client.client_id = 4  # don't collide with daemon (1) or other ad-hoc (2,3)
    await ibkr_client.connect()
    print(f"\n=== Phase 1 smoke test against {ibkr_client.current_account} ===\n")

    # Test 1: malformed LMT must fail fast (<1s) with a structured error.
    t0 = time.time()
    result = await ibkr_client.place_order(
        symbol="AAPL", action="BUY", quantity=1, order_type="LMT"
    )
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"Validation took {elapsed:.2f}s; expected <1s"
    assert result["status"] == "error", f"Expected status=error, got {result['status']}"
    assert "limit_price" in result["message"].lower(), \
        f"Expected limit_price in message, got: {result['message']}"
    print(f"OK Test 1: malformed LMT rejected in {elapsed*1000:.0f}ms")

    # Test 2: server must still respond after Test 1.
    t0 = time.time()
    portfolio = await ibkr_client.get_portfolio()
    elapsed = time.time() - t0
    assert elapsed < 2.0, f"Read took {elapsed:.2f}s after bad order"
    assert ibkr_client.is_connected(), "Lost connection after bad order"
    print(f"OK Test 2: read after bad order in {elapsed*1000:.0f}ms "
          f"({len(portfolio)} positions)")

    # Test 3: valid dry_run order still works.
    result = await ibkr_client.place_order(
        symbol="AAPL", action="BUY", quantity=1, order_type="MKT", dry_run=True
    )
    assert result["status"] == "dry_run", f"Expected dry_run, got {result['status']}"
    print("OK Test 3: valid MKT dry_run returns preview")

    # Test 4: OCA dry_run at group level.
    result = await ibkr_client.place_oca_group(
        oca_group_name="smoke_test_bracket",
        dry_run=True,
        orders=[
            {"symbol": "AAPL", "action": "SELL", "quantity": 1,
             "order_type": "TRAIL", "trail_percent": 5.0, "tif": "GTC"},
            {"symbol": "AAPL", "action": "SELL", "quantity": 1,
             "order_type": "STP", "stop_price": 250.0, "tif": "GTC"},
        ],
    )
    assert result["status"] == "dry_run", f"Expected dry_run, got {result['status']}"
    assert len(result["orders"]) == 2, f"Expected 2 legs, got {len(result['orders'])}"
    print(f"OK Test 4: OCA dry_run validates {len(result['orders'])} legs")

    # Test 5: pathological case — invalid LMT followed by 10 rapid reads.
    # Use `get_swing_status("__NOPE__")` for the post-order reads — it's a
    # pure in-memory lookup with no IB call and no rate-limiter, so the only
    # thing that could slow it down is a wedged event loop. Previously this
    # test used `get_portfolio`, but its `@rate_limit(calls_per_second=1.0)`
    # decorator means 10 sequential calls take ~10s by design — not a wedge.
    await ibkr_client.place_order(
        symbol="AAPL", action="BUY", quantity=1, order_type="LMT"
    )
    t0 = time.time()
    for _ in range(10):
        await ibkr_client.get_swing_status("__NOPE__")
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"10 reads took {elapsed:.2f}s; server appears wedged"
    print(f"OK Test 5: 10 reads after bad order in {elapsed*1000:.0f}ms")

    print("\n=== ALL FIVE ASSERTIONS PASSED ===\n")
    await ibkr_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
