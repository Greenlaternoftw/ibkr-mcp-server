"""Manual end-to-end recovery test for Layer 5a.

Verifies that a swing strategy registered into the state file is recovered by
`resume_strategies_from_state()` on a fresh IBKRClient (the daemon-restart
case), and that `reconcile_on_startup()` correctly prunes any stale order IDs.

Run on the VPS while the systemd daemon is STOPPED:

    sudo systemctl stop ibkr-mcp
    .venv/bin/python tests/test_recovery_manual.py
    sudo systemctl start ibkr-mcp     # bring it back

The script uses isolated tmp state files so it does NOT touch the real
strategies on disk.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from ibkr_mcp_server.client import IBKRClient
from ibkr_mcp_server.swing import SwingState


def _print(label: str, ok: bool, detail: str = "") -> None:
    flag = "OK " if ok else "!! "
    print(f"{flag}{label}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="ibkr-recovery-test-"))
    swing_state_path = tmpdir / "swing.json"
    reversal_state_path = tmpdir / "reversal.json"

    failed = 0

    # === Scenario 1: state survives a "process restart" ===================
    # Create client A, register a swing strategy, persist.
    # Create client B (fresh process) pointed at the same state file —
    # `resume_strategies_from_state` should pick the strategy back up.

    client_a = IBKRClient()
    client_a._swing_state_path = swing_state_path
    client_a._reversal_state_path = reversal_state_path
    client_a._swing_states = {}      # bypass lazy-load
    client_a._reversal_states = {}

    # Connect (so the asyncio task we'll spawn doesn't immediately error).
    # Falls back gracefully if Gateway is down — we don't really need a fill.
    try:
        await client_a.connect()
    except Exception as e:
        print(f"warning: client_a couldn't connect to IBKR ({e!r}). "
              "Recovery logic doesn't require Gateway to test, but we'll "
              "skip the parts that do.")

    result = await client_a.start_swing_strategy(
        symbol="AAPL",
        quantity=10,
        cost_basis=290.0,
        dip_percent=3.0,
        regime_filter_enabled=False,
        recheck_interval_seconds=600,
    )
    _print("client_a.start_swing_strategy", result["status"] == "started", str(result))

    # Cancel the loop task before we leave — we don't want a stray hourly task
    # running in this short script.
    task = client_a._swing_tasks.pop("AAPL", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # State should be on disk now
    state_exists = swing_state_path.exists()
    _print("state file written to disk", state_exists, str(swing_state_path))

    if state_exists:
        on_disk = json.loads(swing_state_path.read_text())
        _print(
            "state file contains AAPL with state=HOLDING",
            on_disk.get("AAPL", {}).get("state") == "HOLDING",
            f"keys={list(on_disk.keys())}",
        )

    if client_a.is_connected():
        try:
            await client_a.disconnect()
        except Exception:
            pass

    # === Scenario 2: fresh client recovers the strategy ===================
    client_b = IBKRClient()
    client_b._swing_state_path = swing_state_path
    client_b._reversal_state_path = reversal_state_path
    client_b._swing_states = None      # force re-load from disk
    client_b._reversal_states = None

    # Use a fake-connected state so resume_strategies_from_state will try to
    # spawn the loop. We immediately cancel anyway.
    client_b._connected = True
    client_b.ib = None     # the loop will error on first tick — that's fine

    resumed = await client_b.resume_strategies_from_state()
    _print(
        "client_b.resume_strategies recovered AAPL",
        "AAPL" in resumed["swing"],
        f"resumed={resumed}",
    )

    # Confirm the state was loaded correctly
    state = client_b._swing_state_dict().get("AAPL")
    if state:
        _print(
            "recovered state preserves quantity + cost_basis",
            state.quantity == 10 and state.cost_basis == 290.0,
            f"qty={state.quantity} cost_basis={state.cost_basis}",
        )
        _print(
            "recovered state preserves dip_percent",
            state.config.dip_percent == 3.0,
            f"dip_percent={state.config.dip_percent}",
        )
    else:
        _print("recovered state preserves AAPL", False, "state missing")
        failed += 1

    # Cancel the recovery-spawned loop task immediately
    task = client_b._swing_tasks.pop("AAPL", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # === Scenario 3: reconcile_on_startup clears stale order IDs ==========
    # Manually plant order IDs in the state, then run reconcile against an
    # IBKR view that has none of them — they should all be cleared.

    from unittest.mock import MagicMock

    class _FakeOrder:
        def __init__(self, oid): self.orderId = oid
    class _FakeStatus:
        def __init__(self, s): self.status = s
    class _FakeTrade:
        def __init__(self, oid, s):
            self.order = _FakeOrder(oid)
            self.orderStatus = _FakeStatus(s)

    state.protective_trail_order_id = 9991
    state.protective_stop_order_id = 9992
    state.dip_buy_order_id = 9993
    state.oca_group = "fake-grp"
    client_b.ib = MagicMock()
    client_b.ib.trades.return_value = []   # IBKR says nothing is open
    client_b._connected = True             # is_connected requires ib.isConnected
    client_b.ib.isConnected.return_value = True

    rec = await client_b.reconcile_on_startup()
    _print(
        "reconcile cleared all 3 stale swing order IDs",
        rec["status"] == "reconciled" and len(rec["cleared"]["swing"]) == 1,
        f"cleared={rec['cleared']}",
    )
    _print(
        "swing state's order IDs are now None",
        all(getattr(state, f) is None for f in
            ("protective_trail_order_id", "protective_stop_order_id", "dip_buy_order_id")),
        f"trail={state.protective_trail_order_id} stop={state.protective_stop_order_id} dip={state.dip_buy_order_id}",
    )
    _print(
        "oca_group cleared after both protective legs vanished",
        state.oca_group is None,
        f"oca_group={state.oca_group}",
    )

    print(f"\nState files left in: {tmpdir}")
    print("=== done ===")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
