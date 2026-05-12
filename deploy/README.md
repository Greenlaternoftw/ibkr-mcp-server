# Deploying the IBKR MCP daemon

This directory contains the artefacts to run the daemon as a 24/7 service
on the VPS that hosts IB Gateway. Two paths:

- **Layer 5a (systemd, no MCP listener)** — daemon runs strategies; you SSH
  in to register/inspect them. Section below.
- **Layer 5b (Docker compose, HTTP MCP transport)** — Gateway + daemon
  come up together; remote clients call MCP tools over HTTPS/bearer auth.
  Section further down.

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

---

# Layer 5b — Docker compose + HTTP MCP transport

Brings up Gateway and the MCP daemon together in two containers, with the
MCP protocol exposed over HTTP at `127.0.0.1:8765/mcp`. Adds bearer auth
for any non-localhost bind.

## Install (one-time)

On the VPS:

```bash
# 1. Stop the systemd daemon if you previously installed Layer 5a — Docker
#    will run the daemon instead.
sudo systemctl stop ibkr-mcp     2>/dev/null || true
sudo systemctl disable ibkr-mcp  2>/dev/null || true

# 2. Make sure .env is configured. The Layer 5b additions are MCP_BIND_HOST,
#    MCP_BIND_PORT, MCP_AUTH_TOKEN — see .env.example. For local-only access
#    via SSH tunnel, leave MCP_BIND_HOST=127.0.0.1 and you can omit the
#    token. For Tailscale or any other shared network, generate a token:
openssl rand -hex 32
#    and put it in MCP_AUTH_TOKEN (mode 600 on the .env file).

# 3. Build and start
cd ~/ibkr-mcp-server/deploy
docker compose up -d --build

# 4. Verify both containers are healthy
docker compose ps
# Expected: ib-gateway and ibkr-mcp both "Up" — ibkr-mcp also "healthy"
# (after ~90 seconds — first start waits on Gateway to log in).

# 5. Health check the MCP transport directly
curl -s http://127.0.0.1:8765/healthz | python -m json.tool
# Expected: {"status":"ok","ibkr_connected":true,"swing_strategies":0,...}
```

## Reaching the MCP endpoint from your laptop

Two options.

### A. SSH tunnel (simplest, no extra software)

```bash
# On your laptop, in a dedicated terminal (keep open):
ssh -N -L 8765:127.0.0.1:8765 trader@your-vps

# Then any local tool that speaks MCP-over-HTTP can connect to
# http://127.0.0.1:8765/mcp on your laptop, which tunnels to the daemon.
```

### B. Tailscale (or any private mesh)

1. Install Tailscale on both the VPS and your laptop, join the same tailnet.
2. In `.env`, change `MCP_BIND_HOST` to the VPS's tailscale IP (e.g.
   `100.x.x.x`). The daemon will refuse to start unless `MCP_AUTH_TOKEN` is
   also set — set one with `openssl rand -hex 32`.
3. `docker compose up -d --force-recreate` to pick up the new bind.
4. From your laptop, the MCP endpoint is at `http://100.x.x.x:8765/mcp`
   with `Authorization: Bearer <token>`.

## Daily operations (Docker version)

- **View logs**: `docker compose logs -f ibkr-mcp`
- **Restart MCP only** (after code update): `docker compose up -d --build ibkr-mcp`
- **Restart everything**: `docker compose restart`
- **Stop everything**: `docker compose down`
- **Inspect strategy state**: state files are inside the `mcp-state` Docker
  volume. Easiest path: `docker compose exec ibkr-mcp ls /home/trader/`

## Differences from Layer 5a (systemd)

| | systemd (5a) | Docker (5b) |
|---|---|---|
| Gateway managed by | You (separate docker run) | Same compose file |
| MCP protocol exposed | ❌ — SSH + Python | ✅ HTTP at `:8765/mcp` |
| State files | `~trader/.ibkr-mcp-*-state.json` (on host) | Docker volume `ibkr-mcp-state` |
| Updates | `git pull && systemctl restart ibkr-mcp` | `git pull && docker compose up -d --build ibkr-mcp` |
| Logs | `journalctl -u ibkr-mcp` | `docker compose logs ibkr-mcp` |

You should pick **one or the other** — running both simultaneously will fight
over `IBKR_CLIENT_ID=1`.
