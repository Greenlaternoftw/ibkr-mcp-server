# Deploying the IBKR MCP daemon (Layer 5a)

This directory contains the artefacts you need to run the daemon as a 24/7
service on the VPS that hosts IB Gateway.

For now (Layer 5a), the daemon is **always-on without an MCP listener** —
the trading strategies tick by themselves; you control them via SSH'd Python
invocations as you have been. Layer 5b will swap stdio for a remote-reachable
HTTP transport.

## What you get from Layer 5a

| Behavior | Without daemon | With `--daemon` |
|---|---|---|
| Loop survives laptop close | ❌ asyncio task dies with your shell | ✅ systemd keeps it alive |
| Loop survives crash | ❌ | ✅ systemd restarts within 5s |
| Loop survives reboot | ❌ | ✅ enabled at boot |
| Fill detection latency | up to `recheck_interval_seconds` (default 1h) | milliseconds (event-driven) |
| State after crash | ✅ disk state preserved, but loop is dead | ✅ daemon recovers state + reconciles vs IBKR |
| Periodic re-evaluation | Only while your shell holds the loop | ✅ hourly, always |

## Install (one-time, on the VPS)

```bash
# 1. Make sure the repo is at the standard path the unit file expects
cd ~ && ls ibkr-mcp-server   # should exist; if not, git clone

# 2. Make sure your .env is in place and has the right vars
cat ~/ibkr-mcp-server/.env
# expected:
#   IBKR_HOST=127.0.0.1
#   IBKR_PORT=4002
#   IBKR_CLIENT_ID=1
#   IBKR_IS_PAPER=true
#   ENABLE_LIVE_TRADING=true
#   MAX_ORDER_SIZE=1000

# 3. Copy the unit file into place and reload systemd
sudo cp ~/ibkr-mcp-server/deploy/systemd/ibkr-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload

# 4. Enable on-boot start
sudo systemctl enable ibkr-mcp

# 5. Start it
sudo systemctl start ibkr-mcp

# 6. Check status
sudo systemctl status ibkr-mcp
# expect: active (running)

# 7. Watch logs (Ctrl+C to detach — daemon keeps running)
journalctl -u ibkr-mcp -f
```

## What you'll see in the logs at startup

```
=== IBKR MCP daemon starting ===
connected to IBKR Gateway
startup reconciliation: {'status': 'reconciled', 'cleared': {...}}
resumed strategies: reversal=['TSLA'] swing=['AAPL']
heartbeat: connection ok           # every 5 minutes
```

If `resumed strategies` is empty, that just means no strategies were active at
the previous shutdown. Use the regular `start_swing_strategy` /
`start_reversal_entry` Python invocations to start one — they'll register
state to the same disk files the daemon reads, and the daemon will be
running their ticks already.

## Daily operations

- **Pause everything**: `sudo systemctl stop ibkr-mcp`
- **Resume**: `sudo systemctl start ibkr-mcp`
- **Restart after a code update**: `sudo systemctl restart ibkr-mcp`
- **See live logs**: `journalctl -u ibkr-mcp -f`
- **See yesterday's logs**: `journalctl -u ibkr-mcp --since yesterday`
- **Inspect strategy state without stopping the daemon**:
  ```bash
  cat ~/.ibkr-mcp-swing-state.json
  cat ~/.ibkr-mcp-reversal-state.json
  cat ~/.ibkr-mcp-regime-state.json
  ```

## Important — starting strategies while the daemon is running

The daemon writes/reads the same JSON state files used by the on-demand Python
sessions you've been using. To start a new strategy:

```bash
# In a normal SSH session on the VPS (not under systemd):
.venv/bin/python - <<'PY'
import asyncio
from ibkr_mcp_server.client import ibkr_client
async def main():
    await ibkr_client.connect()
    await ibkr_client.start_swing_strategy(
        symbol="AAPL", quantity=100, cost_basis=290.0, dip_percent=3.0
    )
    await ibkr_client.disconnect()
asyncio.run(main())
PY

# Then restart the daemon to pick up the new strategy:
sudo systemctl restart ibkr-mcp
```

The restart-to-pick-up-new-strategies step is a Layer 5a wart. Layer 5b will
add an HTTP control plane so you can register a strategy at runtime without a
daemon bounce.

## Gotchas

- **The unit file expects user `trader` and path `/home/trader/ibkr-mcp-server`.**
  If your setup differs, edit the `User=`, `WorkingDirectory=`, `EnvironmentFile=`,
  and `ExecStart=` lines.
- **`.env` permissions matter.** systemd reads it as the `trader` user. If you
  chmod'd it 600 owned by root, systemd won't be able to read it. Owner=trader,
  mode 640 is fine.
- **Daemon needs Gateway alive on the same host.** The unit file declares
  `Requires=docker.service` so systemd boots Docker before the daemon. If
  Gateway is on a different host or you don't use Docker, remove that line.
- **Don't run the daemon AND a separate `python -m ibkr_mcp_server` at the
  same time** — they'd both try to talk to Gateway using the same `IBKR_CLIENT_ID`
  and IBKR will reject the second one. Either use different client IDs in
  separate `.env` files, or stop the daemon before running ad-hoc commands
  that connect to IBKR.
