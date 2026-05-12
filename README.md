# IBKR MCP Server

A Model Context Protocol (MCP) server for Interactive Brokers, with a
six-layer trading toolkit built on top of the original ArjunDivecha fork:
extended order types, a regime filter, a tranched reversal-entry engine, a
swing-trading state machine, and a 24/7 daemon with HTTP transport.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 147 passing](https://img.shields.io/badge/tests-147%20passing-brightgreen.svg)](#testing)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## What this fork adds

Beyond the upstream's read-only account/short-selling tools (which still work),
this fork ships **19 MCP tools** organized in six layers:

| Layer | Capability | Key tools |
|---|---|---|
| 1 | Order placement (10 types) + OCA groups | `place_order`, `place_oca_group` |
| 2 | Regime filter (SMA + ADX + ATR%) with anti-whipsaw smoothing | `check_regime` |
| 3 | 5-signal reversal detection + tranched entry | `check_reversal_signals`, `start_reversal_entry`, `stop_reversal_entry`, `get_reversal_status` |
| 4 | Swing-trading state machine (HOLDING ↔ FLAT) | `start_swing_strategy`, `stop_swing_strategy`, `get_swing_status`, `update_swing_params` |
| 5a | Always-on daemon + systemd + event-driven fills | (`--daemon` CLI mode) |
| 5b | HTTP/MCP transport + Docker compose + bearer auth | (`--transport http` CLI mode) |

All trading is gated by `ENABLE_LIVE_TRADING` and `MAX_ORDER_SIZE`, defaults to
paper, and supports `dry_run=true` for validation without transmission. See
[STRATEGIES.md](STRATEGIES.md) for the design rationale behind each layer.

## Quick start

### Prerequisites

- Python 3.10+ (3.12 recommended)
- An IBKR account with TWS or IB Gateway running on the same host (paper
  trading recommended)
- `uv` (recommended) or pip

### Local install

```bash
git clone https://github.com/Greenlaternoftw/ibkr-mcp-server.git
cd ibkr-mcp-server

# uv is dramatically faster than pip and handles the prerelease pin on
# pandas_ta automatically (declared in pyproject.toml).
uv venv --python 3.12
uv pip install -e ".[dev]"

cp .env.example .env
# Edit .env — at minimum set IBKR_PORT (7497 TWS paper, 4002 Gateway paper)
# and decide on ENABLE_LIVE_TRADING + MAX_ORDER_SIZE.

# Sanity check
.venv/bin/python -m ibkr_mcp_server --test
# Expected: ✅ Connection successful, account listed, 19 tools loaded.

# Unit tests
.venv/bin/python -m pytest -q
# Expected: 147 passed.
```

### Production deploy (VPS)

For a 24/7 always-on setup, see **[deploy/README.md](deploy/README.md)**. Two
paths:

- **Layer 5a (systemd)** — daemon runs strategies, you SSH in to control.
- **Layer 5b (Docker compose)** — Gateway + daemon in containers, MCP
  reachable via HTTPS/bearer auth from your laptop (over SSH tunnel or
  Tailscale).

## Worked example: AAPL through the full pipeline

Suppose AAPL is in a steep drawdown. The intended workflow:

**Step 1 — Regime check.** Is the broader picture safe to dip-buy?

```python
r = await client.check_regime("AAPL")
# {"enabled": false, ...}  → regime filter says NO, don't enter long
```

If `enabled=false` for 3 consecutive days (`sticky_disabled=true`), the
regime filter is firmly saying "downtrend, don't buy dips here."

**Step 2 — Start watching for a reversal.**

```python
await client.start_reversal_entry(
    symbol="AAPL", total_dollars=30000,
    tranche_count=3, tranche_sizing="weighted",      # 20/30/50
    min_signals_for_entry=3, signal_window_days=3,
    stall_timeout_days=10,
)
```

The reversal daemon ticks hourly, watching for 3-of-5 signals (RSI
divergence, RSI > 30 cross, MACD bullish crossover, higher swing-low,
volume surge) to hold for 3 consecutive days. When they do, it places
Tranche 1 (20% of $30k = $6k). At 4 signals it adds Tranche 2 (30% = $9k);
at 5, Tranche 3 (50% = $15k).

**Step 3 — Convert to swing-loop management once an entry's in.**

```python
await client.stop_reversal_entry("AAPL", action="convert_to_swing_loop")
```

This computes the weighted-average fill price across tranches, calls
`start_swing_strategy` with that as `cost_basis`, and from this point the
position is managed by the swing loop:

- Places an OCA pair: trailing SELL (2 × ATR(14) trail) + hard STP at cost
  basis.
- On sell fill → 24-hour cooldown → places a LMT BUY at the dip price
  (3% below the sell).
- On buy fill → re-arms the OCA pair.
- Throughout, the regime filter is consulted; if regime turns off, pending
  dip-buys are cancelled (protective sells stay).

## All 19 MCP tools

### Layer 0 — pre-existing read-only

| Tool | Purpose |
|---|---|
| `get_portfolio` | Current positions and P&L |
| `get_account_summary` | Account balances and key metrics |
| `switch_account` | Switch between subaccounts |
| `get_accounts` | List all accessible accounts |
| `check_shortable_shares` | Short-sale availability |
| `get_margin_requirements` | Margin per-symbol |
| `short_selling_analysis` | Bulk shortable + margin analysis |
| `get_connection_status` | IBKR + paper-trading status |

### Layer 1 — order placement

| Tool | Order types supported |
|---|---|
| `place_order` | MKT, LMT, STP, STP LMT, TRAIL, TRAIL LIMIT, LOO, MOO, LOC, MOC |
| `place_oca_group` | 2+ linked orders sharing an OCA group |

Every order respects `ENABLE_LIVE_TRADING` and `MAX_ORDER_SIZE`; supports
`dry_run=true` for validation; returns a plain-English `intent` string in
its preview.

### Layer 2 — regime filter

| Tool | Returns |
|---|---|
| `check_regime` | per-gate breakdown (SMA, ADX, ATR%) + consecutive-days counter + `sticky_enabled`/`sticky_disabled` |

### Layer 3 — reversal entry

| Tool | Purpose |
|---|---|
| `check_reversal_signals` | Stateless: count of 5 signals + recommended tranche |
| `start_reversal_entry` | Register a tranched plan with hourly tick |
| `stop_reversal_entry` | Cancel / liquidate filled / convert to swing loop |
| `get_reversal_status` | Current state, filled tranches, last signal count |

### Layer 4 — swing-trading loop

| Tool | Purpose |
|---|---|
| `start_swing_strategy` | Register a HOLDING-state position for managed dip-buying |
| `stop_swing_strategy` | Cancel all open orders, stop the loop |
| `get_swing_status` | State, open orders, last fill, last regime check |
| `update_swing_params` | Adjust trail/dip/floor/etc. without stopping |

## Configuration reference

All settings are env vars, loaded from `.env` (or `EnvironmentFile=` for
systemd). See [.env.example](.env.example) for defaults.

### Connection

| Variable | Default | Notes |
|---|---|---|
| `IBKR_HOST` | `127.0.0.1` | Set to `::1` only if Gateway listens on IPv6 only |
| `IBKR_PORT` | `7497` | 7497=TWS paper, 7496=TWS live, 4002=Gateway paper, 4001=Gateway live |
| `IBKR_CLIENT_ID` | `1` | Must be unique per process per Gateway |
| `IBKR_IS_PAPER` | `true` | Cosmetic; the actual paper/live decision is the port |

### Safety gates

| Variable | Default | Notes |
|---|---|---|
| `ENABLE_LIVE_TRADING` | `false` | When false, every order returns `status="blocked"` |
| `MAX_ORDER_SIZE` | `1000` | Per-order share cap. Applied before transmission |
| `REQUIRE_ORDER_CONFIRMATION` | `true` | Currently informational; will gate UI prompts in a future layer |

### Layer 5b — HTTP transport

| Variable | Default | Notes |
|---|---|---|
| `MCP_BIND_HOST` | `127.0.0.1` | Localhost binds skip auth; any other bind requires `MCP_AUTH_TOKEN` (startup will refuse otherwise) |
| `MCP_BIND_PORT` | `8765` | Tunneled via SSH or reached via Tailscale |
| `MCP_AUTH_TOKEN` | (empty) | Generate with `openssl rand -hex 32`. Sent as `Authorization: Bearer <token>` |

### Strategy tuning

`check_regime`, `start_reversal_entry`, and `start_swing_strategy` all accept
config overrides as kwargs. Defaults:

| Layer | Parameter | Default | What it controls |
|---|---|---|---|
| 2 | `adx_threshold` | 25.0 | Trend-strength gate (lower = more selective) |
| 2 | `atr_lookback` | 100 | Days of trailing ATR% for the calm-vol gate |
| 2 | `sma_period` | 50 | SMA length for the trend gate |
| 2 | `sma_lookback_days` | 5 | "rising" = today's SMA vs SMA `n` days ago |
| 2 | `require_all_gates` | true | If false, 2/3 passing is enough |
| 2 | `smoothing_days` | 3 | Consecutive agreements before `sticky_*` flips |
| 3 | `tranche_count` | 3 | Number of equal/weighted tranches |
| 3 | `tranche_sizing` | "equal" | or "weighted" (20/30/50 for n=3) |
| 3 | `min_signals_for_entry` | 3 | Threshold for first tranche |
| 3 | `signal_window_days` | 3 | Anti-whipsaw — must hold for N days |
| 3 | `stall_timeout_days` | 10 | Abort if no progress |
| 3 | `protective_stop_atr_multiple` | 2.0 | Stop-and-wait stop = entry - N × ATR |
| 4 | `trail_atr_multiplier` | 2.0 | OCA trail = N × ATR(14) |
| 4 | `floor_offset` | 0.0 | OCA STP = cost_basis - floor_offset |
| 4 | `dip_amount` / `dip_percent` | — | Exactly one required — re-entry below the last sell |
| 4 | `cooldown_hours` | 24 | No opposite-direction order in this window after a fill |
| 4 | `require_volume_confirmation` | false | If true, gate dip-buys on volume > avg × multiplier |
| 4 | `volume_threshold_multiplier` | 1.0 | What "above average" means for the volume gate |

## Claude integration

### Local stdio mode (single process, ad-hoc)

For Claude Desktop or Claude Code on the same machine as Gateway:

```json
{
  "mcpServers": {
    "ibkr": {
      "command": "python",
      "args": ["-m", "ibkr_mcp_server"],
      "cwd": "/path/to/ibkr-mcp-server"
    }
  }
}
```

### Remote HTTP mode (production daemon)

Once you've deployed via systemd or Docker (see [deploy/README.md](deploy/README.md)):

- SSH-tunnel from your laptop: `ssh -N -L 8765:127.0.0.1:8765 trader@vps`
- Or use a Tailscale IP with a bearer token (refused at startup if
  non-localhost bind has no token)
- Point your MCP client at `http://127.0.0.1:8765/mcp`

## Recommended workflow

This system can place orders. Treat it like the loaded weapon it is.

1. **Run all unit tests** — `pytest -q` returns 147 passing.
2. **Paper-trade for ≥ 4 weeks.** Run the daemon on the VPS against
   `IBKR_IS_PAPER=true` and `ENABLE_LIVE_TRADING=true`. Watch the regime
   filter accumulate readings on real markets; trigger at least one full
   swing cycle (HOLDING → FLAT → HOLDING) manually. Read every log entry.
3. **Live with 1/10 normal size for ≥ 4 weeks.** Switch to live ports
   (7496/4001) and set `MAX_ORDER_SIZE` to 10% of your eventual size cap.
   Confirm the daemon survives the IBKR-forced nightly logout (~23:59 ET)
   without manual intervention.
4. **Scale to full size.** Only after both windows pass clean.

There is no fast path. Trading systems fail in the long tail of
edge cases.

## Risk disclaimer

This software places real orders against real markets. It can lose real
money. Known risks specific to the strategies it implements:

- **Whipsaw losses.** The swing loop sells on a trail trigger and buys
  back at a dip. If price oscillates around your stop-trail boundary,
  you can be repeatedly bought-and-stopped, each cycle eroding capital
  via spreads, commissions, and the dip-gap (you re-buy lower than you
  sold, but not low enough to cover round-trip costs over many cycles).
- **Gap risk.** Overnight gaps below your hard stop will execute at the
  open, often much worse than the stop price. ATR-based stops mitigate
  but don't eliminate this.
- **Drift in cost basis.** Each swing cycle updates cost basis to the
  most recent buy price. Over many cycles the floor drifts; a strict
  cost-basis floor isn't a fixed price.
- **Tax implications.** Frequent buys and sells generate short-term
  capital gains. The system has no awareness of wash-sale rules.
- **Reversal false positives.** The 5-of-5 signal threshold for full
  position size only fires on real bottoms ~30-50% of the time. Plan
  for at least one stop-and-wait per signal cluster.
- **Daemon outages.** If the daemon dies between ticks, its state is
  preserved on disk and recovers on restart — but any fills that
  happened *during* the outage are detected only on the next tick
  (or next event subscription if event-driven mode is working).
- **IBKR API quirks.** Order rejections (e.g. "Order TIF set to DAY
  based on order preset" — Layer 1 hit this) can cancel orders the
  state machine thinks are live. Reconciliation runs on daemon startup;
  in-flight discrepancies are caught on the next tick.

**No backtest is included.** Don't infer that the strategy parameters
shipped as defaults have been historically optimized — they haven't.
They're sensible-sounding starting points. Walk-forward-test on your
own data before believing any of them.

## Testing

```bash
pytest -q                          # full suite, 147 tests
pytest tests/test_orders.py -v     # one layer at a time
python tests/test_recovery_manual.py    # end-to-end daemon-recovery proof
python tests/smoke_paper.py            # 5 acceptance scenarios, dry-run
python tests/smoke_paper.py --live     # the same, transmitted to paper
```

The `smoke_paper.py` script needs a running Gateway and an active SSH
tunnel (or daemon-mode HTTP transport). See [layer-1 instructions](../ibkr-mcp-instructions/layer-1-orders-and-oca.md)
for the 7 manual acceptance scenarios you should walk through before
trusting any layer.

## License

MIT.

## Acknowledgements

Built on top of [ArjunDivecha/ibkr-mcp-server](https://github.com/ArjunDivecha/ibkr-mcp-server).
The original repo's read-only account/short-selling tools survive
unchanged; this fork adds the order-placement and strategy machinery.
