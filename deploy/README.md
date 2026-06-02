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

## Auto-recovery + daily maintenance (recommended)

Two scripts in `scripts/` keep the daemon connected without manual intervention.

### What they do

- **`ibkr-watchdog.sh`** — every 5 minutes via cron, checks the chain:
  1. Gateway's API port (4002) listening
  2. Daemon's HTTP `/healthz` endpoint responding
  3. `/healthz` reporting `ibkr_connected=true`

  If any check fails, restarts what's broken (Gateway and/or the daemon).
  Logs everything to `/home/trader/ibkr-watchdog.log`. Idempotent and
  lock-guarded so concurrent runs can't conflict.

- **`ibkr-daily-restart.sh`** — once a day at 04:30 UTC, bounces Gateway
  and then the daemon. Catches IBC accumulation bugs that cause the silent
  port-closed-after-nightly-logout failure mode by never letting Gateway
  state age more than 24 hours.

### Install (one-shot)

On the VPS:

```bash
bash /home/trader/ibkr-mcp-server/scripts/install-ops.sh
```

This will:
1. `chmod +x` both scripts
2. Add a sudoers rule (`/etc/sudoers.d/ibkr-ops`) so cron can
   `systemctl restart ibkr-mcp` without a password prompt
3. Add two cron entries (skipping any that already exist)
4. Print the resulting crontab

Then verify:
```bash
tail -f /home/trader/ibkr-watchdog.log
# wait 5 minutes; you should see one "OK: chain healthy" line on the first
# run within the first 5-minute window of an hour.
```

### Configuration

Both scripts read environment variables for paths/timeouts. Defaults
match the layout described elsewhere in this README. Override by
prepending vars to the cron line, e.g.:

```cron
*/5 * * * * IBKR_WATCHDOG_LOG=/var/log/ibkr.log /home/trader/ibkr-mcp-server/scripts/ibkr-watchdog.sh
```

Available overrides for the watchdog:

| Env var | Default | Purpose |
|---|---|---|
| `IBKR_WATCHDOG_LOG` | `/home/trader/ibkr-watchdog.log` | Where to log |
| `IBKR_WATCHDOG_ENV_FILE` | `/home/trader/ibkr-mcp-server/.env` | Where to read `MCP_AUTH_TOKEN` from |
| `IBKR_WATCHDOG_GATEWAY_COMPOSE` | `/home/trader/ibkr-stack/docker-compose.yml` | Gateway compose file |
| `IBKR_WATCHDOG_HEALTHZ_URL` | `http://127.0.0.1:8765/healthz` | Daemon health endpoint |
| `IBKR_WATCHDOG_GATEWAY_PORT` | `4002` | Port to check for listening |
| `IBKR_WATCHDOG_GATEWAY_WAIT` | `90` | Seconds to wait after Gateway restart |
| `IBKR_WATCHDOG_DAEMON_WAIT` | `5` | Seconds to wait after daemon restart |
| `IBKR_WATCHDOG_STATE_FILE` | `/tmp/ibkr-watchdog.last-state` | Tracks last-known state so we only alert on transitions |

Daily-restart accepts `IBKR_OPS_LOG`, `IBKR_GATEWAY_COMPOSE`,
`IBKR_GATEWAY_WAIT` with equivalent meanings.

### Disable / uninstall

```bash
crontab -e                            # remove the two ibkr-* lines
sudo rm /etc/sudoers.d/ibkr-ops
```

---

## Phone alerts (ntfy.sh)

The daemon will push an iOS/Android notification to your phone when:

1. **The daemon loses its IBKR connection** — fired from inside the daemon by
   `IBKRClient._on_disconnect`. De-duplicated so a single physical drop produces
   one alert even if ib_async re-emits the event during retries.
2. **The daemon HTTP is unreachable** ("hang up of server") — fired from
   `scripts/ibkr-watchdog.sh`. The daemon obviously can't notify when it's the
   wedged thing, so the watchdog doubles as the alarm.
3. **The chain recovers** — single "✅ IBKR reconnected" when the daemon's
   connection is restored, and a "chain recovered" message from the watchdog
   when whatever it was healing comes back.

Watchdog alerts are debounced via a `/tmp/ibkr-watchdog.last-state` token so
a 6-hour outage is one alert, not 72.

### Setup (5 minutes)

1. Install the **ntfy** app on your phone (free, App Store / Play Store).
2. Pick a topic name you can subscribe to. Topic names are PUBLIC — anyone
   who knows the topic can read its messages, so make it unguessable:
   ```bash
   echo "ibkr-$(openssl rand -hex 4)"
   # -> e.g. ibkr-a3f7c129
   ```
3. In the ntfy app, tap "+" and subscribe to that topic.
4. On the VPS, edit `.env`:
   ```ini
   NOTIFY_ENABLED=true
   NTFY_URL=https://ntfy.sh
   NTFY_TOPIC=ibkr-a3f7c129     # whatever you generated above
   ```
5. Restart the daemon so it picks up the new env:
   ```bash
   sudo systemctl restart ibkr-mcp
   ```
6. Verify with a test push:
   ```bash
   curl -d "test alert from VPS" \
        -H "Title: IBKR alerts wired" \
        -H "Priority: 3" \
        https://ntfy.sh/ibkr-a3f7c129
   ```
   You should get a push within ~2 seconds.

### Verify the watchdog path

Force a "daemon down" alert by stopping the daemon briefly. The watchdog
will detect it on its next 5-minute tick and:
- POST an alert to your topic (title "IBKR daemon HTTP wedged")
- Restart the daemon
- POST a "chain recovered" alert on the following tick

```bash
sudo systemctl stop ibkr-mcp
# wait for the next */5 cron tick, then check:
tail -n 20 /home/trader/ibkr-watchdog.log
```

If you see the watchdog log entries but no push: the daemon's notify path
is failure-silent on purpose, so confirm `NOTIFY_ENABLED` and `NTFY_TOPIC`
are set in `.env` and that the manual `curl` test above worked.

### Disabling alerts

`NOTIFY_ENABLED=false` in `.env` + daemon restart. The watchdog reads the
same `.env` so a single toggle disables both paths.

---

## Layer 7 — In-house chat wrapper

A small chat app served at `http://<vps>:8765/chat` that calls **Anthropic
API directly** instead of going through Claude Desktop / iOS / web. The
reason it exists: those consumer products apply a safety overlay that
refuses to invoke destructive trading tools regardless of how the
operator-controlled daemon is configured. By calling the API directly
with our own system prompt, the model honors the operator's intent and
the daemon's confirmation gate becomes the actual safety mechanism — not
the model's editorial judgment.

### Setup (10 minutes)

1. **Get an Anthropic API key.** Go to
   [console.anthropic.com](https://console.anthropic.com/), Settings →
   API Keys → "Create Key". This is separate from your Claude.ai
   subscription. The key starts with `sk-ant-api...`. You only see it
   once — copy it immediately.

2. **Set a spend cap on the console.** Anthropic API is metered. A
   $100/month cap is a sane starting point for chat-only use; lower if
   you want to be conservative.

3. **Add the new env vars to `.env` on the VPS:**

   ```ini
   CHAT_ENABLED=true
   ANTHROPIC_API_KEY=sk-ant-api03-...
   ANTHROPIC_MODEL=claude-sonnet-4-5
   CHAT_MAX_ITERATIONS=12
   ```

4. **Install the new Python dep:**

   ```bash
   cd /home/trader/ibkr-mcp-server
   git pull
   .venv/bin/pip install -r requirements.txt   # adds `anthropic`
   sudo systemctl restart ibkr-mcp
   ```

5. **Open the chat UI from anywhere on your Tailnet:**

   On your laptop or phone browser:
   ```
   http://<vps-tailscale-ip>:8765/chat?token=<MCP_AUTH_TOKEN>
   ```

   The token is stripped from the URL after first load and saved in
   localStorage. Bookmark the URL **without** the `?token=...` after
   that — re-using it would put the token in browser history.

6. **Phone: install as a PWA.** In Safari → Share → Add to Home Screen.
   The icon appears alongside native apps and opens in full-screen mode.

### What changes for you

- **Claude refusing to place orders → gone.** The system prompt
  explicitly authorizes destructive tool calls and explains the
  confirmation gate. The agent loop surfaces the daemon's
  `needs_confirmation` previews and waits for your "confirm".
- **Existing surfaces still work.** Claude Desktop / iOS / Code can still
  hit the MCP endpoint at `:8765/mcp`. Use whichever interface you
  prefer for which task.
- **Cost:** roughly $0.01-0.02 per chat turn at Sonnet pricing. At
  ~100 turns/day that's ~$30-50/month. The console cap protects you.

### Tuning

| Env var | Purpose |
|---|---|
| `CHAT_ENABLED` | Master switch. False disables the `/chat` endpoint (returns 503). |
| `ANTHROPIC_API_KEY` | The API key from console.anthropic.com. |
| `ANTHROPIC_MODEL` | Defaults to `claude-sonnet-4-5`. Use `claude-haiku-4-5` to cut cost ~5×; use `claude-opus-4-5` for serious analysis. |
| `CHAT_MAX_ITERATIONS` | Cap on consecutive tool-call iterations in one user turn. Default 12. Raise if the agent legitimately needs more chained calls; lower as a paranoid budget control. |

The system prompt lives at `ibkr_mcp_server/chat/prompts.py`. Edit
freely and restart the daemon. Common tweaks:

- Make the model more verbose for portfolio explanations
- Customize how the model formats numbers (currency, percentages)
- Add personal trading-strategy preferences ("I prefer trailing stops
  over hard stops; flag anything that would set a hard stop")

### Architecture notes

- The chat app shares the daemon's HTTP transport (port 8765) and
  bearer-auth middleware. No new ports to firewall.
- One process, shared `IBKRClient` instance — tool calls from chat hit
  the same code path as MCP tool calls. The confirmation gate fires
  identically.
- Conversation lives in browser localStorage (Phase 1). Server-side
  persistence is Phase 2 work.
- All tool dispatch goes through the existing `tools.call_tool` MCP
  handler, so a tool that works in Claude Desktop works in chat and
  vice versa.

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
