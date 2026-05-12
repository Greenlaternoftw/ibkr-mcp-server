# Usage guide — every function with a plain-English example

This is the operator's quick-reference. For each of the 20 MCP tools you have
in Claude Desktop, this page shows:

  - What it does
  - When you'd use it
  - An example prompt you can paste directly into Claude Desktop

For deeper theory on *why* each layer is designed the way it is, see
[STRATEGIES.md](STRATEGIES.md).

> Anywhere a screenshot would help, I've marked it as `[SCREENSHOT: ...]`.
> Capture them yourself as you go and paste them in below the relevant section.

---

## The big picture

```
┌──────────────────────────────────────────────────────────────────┐
│  YOUR LAPTOP                                                     │
│                                                                  │
│  Claude Desktop (chat UI)                                        │
│     │                                                            │
│     │  via mcp-remote (stdio→HTTP bridge)                        │
│     │                                                            │
│     ▼                                                            │
└─────┼────────────────────────────────────────────────────────────┘
      │ Tailscale (encrypted private network, 100.x.y.z)
┌─────▼────────────────────────────────────────────────────────────┐
│  VPS                                                             │
│                                                                  │
│   ┌──────────────────────┐    ┌────────────────────────────┐     │
│   │  ibkr-mcp daemon     │    │  IB Gateway (Docker)       │     │
│   │  (systemd, always-on)│◄──►│  paper account, port 4002  │     │
│   └──────────────────────┘    └────────────────────────────┘     │
│              │                          │                        │
│              │ JSON state files          │                        │
│              ▼                          │                        │
│   ~/.ibkr-mcp-{regime,reversal,        │                        │
│             swing}-state.json          │                        │
└──────────────────────────────────────────┼────────────────────────┘
                                           │
                                           ▼
                              IBKR's servers (paper or live)
```

[SCREENSHOT: Tailscale menu bar showing VPS connected]
[SCREENSHOT: Claude Desktop with 🛠️ tool list showing 20 ibkr tools]

---

## The 6 layers at a glance

| Layer | Purpose | When you use it |
|---|---|---|
| 1 — Orders + OCA | Place any order type, plus linked groups | Building blocks; rarely called directly |
| 2 — Regime filter | Is the market "calm-uptrend" right now? | Before deciding to dip-buy anything |
| 3 — Reversal entry | Watch for a bottom; scale in across 3 tranches | When a symbol is in drawdown and you want to catch the recovery |
| 4 — Swing loop | Trailing-protect + dip-rebuy a position you hold | After any entry, to manage the position automatically |
| 5a — Daemon | The always-on process that runs the loops | Invisible; just leave it running |
| 5b — HTTP transport | Lets Claude Desktop talk to the daemon | Invisible; the setup you completed |

---

## Account and connection (no trading actions)

### `get_connection_status`

What it does: reports whether the daemon is connected to IBKR Gateway and which account is active.

When to use: when you suspect something's wrong before placing any order.

> *Check the IBKR connection status.*

Expected: account ID, paper-trading flag, connected=true.

[SCREENSHOT: Claude Desktop response showing connection ok]

---

### `get_accounts` + `switch_account`

If your IBKR login manages multiple subaccounts.

> *List my IBKR accounts.*
> *Switch to account DU7654321.*

---

### `get_portfolio` + `get_account_summary`

Current positions and balances.

> *Show me my current paper-account portfolio.*
> *What's my account summary right now? Buying power, equity, P&L.*

---

## Layer 1 — Order placement

### `place_order` (10 order types)

What it does: places a single order. Supports MKT, LMT, STP, STP LMT,
TRAIL, TRAIL LIMIT, LOO, MOO, LOC, MOC.

When to use: ad-hoc trades that aren't part of a managed strategy. For
position management, use the Layer 4 swing loop instead.

Examples:

> *Place a market buy for 1 share of AAPL.*
> *Place a limit sell of 100 NVDA at $250.*
> *Place a trailing stop sell of 100 AAPL with a $2 trail, GTC.*
> *Place a stop sell on TSLA: 50 shares, trigger at $400, GTC.*
> *Dry run: would a MOC sell of 200 AAPL be valid right now?*

Useful flags:
- "dry run" → validates without transmitting (returns the intent only)
- "GTC" → order persists across days
- "outside RTH" → also valid in pre/after-market

**Safety gates always active:**
- `ENABLE_LIVE_TRADING=false` → all orders return `status: blocked`
- `quantity > MAX_ORDER_SIZE` → returns `status: error`

[SCREENSHOT: a trailing stop sell preview from Claude Desktop]

---

### `place_oca_group`

What it does: places 2+ linked orders that cancel each other if any fills (One-Cancels-All).

When to use: protective sell pairs (trailing + hard stop), bracket orders, A-or-B entry scenarios.

> *Place a protective OCA pair on my AAPL: a 5% trailing sell of 100 shares and a hard stop sell at $250, both GTC.*

You'll rarely call this directly — `start_swing_strategy` builds OCA pairs for you.

---

## Layer 2 — Regime filter

### `check_regime`

What it does: evaluates 3 gates on a symbol and tells you if conditions
favor dip-buying right now.

The 3 gates (all must pass for `enabled=true`):
1. **SMA(50) trend rising** — today vs 5 trading days ago
2. **ADX(14) below threshold** — default 25; lower = more selective for calm uptrend
3. **ATR%(14) calm** — recent volatility below its 100-day average

When to use: before placing any new long-side trade. The system also
consults this internally before every swing-loop dip-buy.

> *Check the regime filter on AAPL.*
> *What's the regime status for AAPL, NVDA, and TSLA — show me each gate.*
> *Check AAPL's regime with a stricter ADX threshold of 20.*

The `consecutive_days_enabled` counter prevents whipsaw. Real production
strategies should look at `sticky_enabled` (true after 3 consecutive
matching days) rather than the bare `enabled` flag.

```
Output structure:

        ┌─────────────────────────────┐
        │  AAPL — $294.28             │
        │                             │
        │  trend_rising      ✓        │  SMA50 263.96 > 261.82 (5d ago)
        │  trend_strength    ✓        │  ADX 23.79 < 25
        │  volatility_calm   ✓        │  ATR% 2.10% < avg 2.14%
        │                             │
        │  enabled       = true       │
        │  consec_days   = 1          │
        │  sticky_enabled = false     │  (needs 3 days of agreement)
        └─────────────────────────────┘
```

[SCREENSHOT: regime gate breakdown for three symbols]

---

## Layer 3 — Reversal entry

State machine:

```
   WATCHING ──signals≥3, held 3 days──► PARTIALLY_FILLED (tranche 1 placed)
       ▲                                        │
       │                                signals stay ≥4 ─┐
       │                                                 ▼
       │                              PARTIALLY_FILLED (tranche 2)
       │                                                 │
       │                                signals stay ≥5 ─┤
       │                                                 ▼
       │                                       COMPLETE (all 3 filled)
       │
       └─signals drop after T1── STALLED ──signals return──┐
                                    │                       │
                                    │                       │
                              protective stop at         re-arm
                              entry − 2×ATR(14)
                                    │
                            stall_timeout_days     ▼
                                    └────► ABORTED
```

The 5 reversal signals (count how many fire):
1. Bullish RSI divergence
2. RSI crossed above 30 (in last 3 days)
3. MACD bullish crossover (in last 3 days)
4. Higher swing-low confirmed (5-bar pivots)
5. Volume surge (1.5× the 20-day average on an up-day)

---

### `check_reversal_signals`

What it does: stateless report of how many of the 5 signals are firing right now.

When to use: scanning to see if anything is setting up.

> *Check reversal signals on TSLA.*
> *Which of AAPL, NVDA, TSLA, AMD are showing 3+ reversal signals today?*

[SCREENSHOT: signal flag breakdown — typically `0/5` or `1/5` on calm days]

---

### `start_reversal_entry`

What it does: starts a managed tranched entry plan. The daemon watches
hourly; tranches fire when the signal threshold is met for `signal_window_days`.

When to use: a symbol you want to long has just bottomed (or you suspect
it has) and you want disciplined scale-in instead of lump-sum.

> *Start a reversal entry on TSLA with $30,000 total, three equal tranches, default settings.*
> *Start a reversal entry on AMD with $20k total, weighted sizing (20/30/50), abort after 14 days of no progress.*

Defaults: 3 equal tranches, 3 signals to start, 1 additional signal per
later tranche, 3-day signal window, 10-day stall timeout, protective stop
at 2×ATR below the most recent tranche fill.

---

### `stop_reversal_entry`

What it does: stop a running reversal plan with one of three actions:
- `cancel` — stop watching, leave filled tranches alone
- `liquidate_filled` — market-sell anything filled
- `convert_to_swing_loop` — hand the filled position to the swing loop for ongoing management

> *Cancel the TSLA reversal entry. Leave filled tranches alone.*
> *Stop the TSLA reversal and liquidate everything we filled.*
> *Stop the TSLA reversal entry and convert the filled tranches into a swing strategy with 3% dip re-entry.*

---

### `get_reversal_status`

> *What's the status of my TSLA reversal entry? How many tranches have filled?*

---

## Layer 4 — Swing loop (the daily workhorse)

State machine:

```
              start_swing_strategy(symbol, qty, cost_basis, dip_*)
                              │
                              ▼
              ┌─────────► HOLDING ◄─────────────┐
              │              │                  │
              │              │ place OCA pair:  │
              │              │  TRAIL SELL +    │  dip-buy fills
              │              │  STP SELL at     │
              │              │  cost − floor    │  (24h cooldown after
              │              │                  │   any fill)
              │              ▼                  │
              │     either sell fills           │
              │              │                  │
              │              ▼                  │
              │            FLAT ────────────────┘
              │              │  place LMT BUY at
              │              │  sell_price × (1 - dip_pct)
              │              │
              └──────────────┘  (regime must still be enabled
                                 to place the dip-buy)
```

---

### `start_swing_strategy`

What it does: registers a position for managed swing trading. The daemon
immediately places a protective OCA pair on the position and from then on
auto-manages: when the OCA fires, places a dip-buy; when the dip-buy fills,
places a new OCA.

When to use: you've taken a position (manually or via reversal handoff)
and want it managed without further babysitting.

Required: `symbol`, `quantity`, `cost_basis`, and EITHER `dip_amount` OR `dip_percent`.

> *Start a swing strategy on AAPL: 1 share at $294, 2% dip re-entry, $5 floor.*

Translation: position is 1 share at cost $294. Trail = 2×ATR (default).
Hard floor at $289 ($294 − $5). When a protective sell fires, queue a LMT
BUY at 2% below the sell price (~$288 if sold at $294).

> *Manage my 100 AAPL position with $260 cost basis, use a 1.5×ATR trail, dip back in at $5 lower.*

Useful kwargs:
- `trail_atr_multiplier` (default 2.0)
- `floor_offset` (default 0; positive = floor below cost)
- `regime_filter_enabled` (default true; when regime is off, no dip-buys are queued)
- `require_volume_confirmation` (default false)
- `recheck_interval_seconds` (default 3600 = 1 hour)

---

### `update_swing_params` (self-healing)

What it does: live-update a running swing strategy's tuning. If you change
a *structural* parameter (`trail_atr_multiplier`, `floor_offset`,
`dip_amount`, `dip_percent`), the daemon immediately cancels the broker-side
orders that were placed with the old values and re-places them with the
new values — within ~1 second of the chat message.

When to use: adjusting risk, reacting to news, tightening a stop after a
big move up.

> *Update the AAPL swing strategy: tighten the floor to $5 below cost.*
> *Change AAPL's trail multiplier to 1.5×ATR.*
> *Update AAPL — dip percent should be 3 instead of 2.*
> *Turn off the regime filter on AAPL for now.*

Response includes `structural_changed: true/false` so you know whether
broker-side orders were updated.

---

### `stop_swing_strategy`

What it does: cancels all open swing orders and stops the loop. Position
(if you still own shares) is left alone — you'd manage that manually.

> *Stop the AAPL swing strategy.*

---

### `get_swing_status`

What it does: returns current state, open order IDs, OCA group, config,
last fill, last regime check.

> *What's the status of my AAPL swing strategy?*
> *Show me everything about my running swing strategies.*

---

### `tick_now`

What it does: forces an immediate strategy tick instead of waiting for the
next scheduled one (default hourly).

When to use: testing, debugging, reacting to news without waiting an hour.

> *Tick the AAPL swing strategy now.*
> *Force an immediate tick on my TSLA reversal entry.*

Returns the action the tick took (`hold`, `place_protective_oca`,
`place_dip_buy`, etc.).

---

## Short-selling and margin tools (from upstream)

These exist but aren't part of the strategy stack.

### `check_shortable_shares`

> *Can I short 200 shares of GME right now? How many are available?*

### `get_margin_requirements`

> *What's the margin requirement to short 100 TSLA?*

### `short_selling_analysis`

> *Run a short-selling analysis on GME, AMC, and BBBY — show availability, borrow rates, margin.*

---

## Common workflows

### Workflow 1 — "I want to dip-buy AAPL but I'm not sure if it's a good time"

> *Check the regime filter on AAPL. Then check the reversal signals. Should I be looking to long this?*

Claude will call `check_regime` and `check_reversal_signals`, then describe the picture.

---

### Workflow 2 — "I want to manage a position I just took"

> *I just bought 100 AAPL at $290. Start a swing strategy: 1.5×ATR trail, $5 floor, 3% dip re-entry, with regime filter on.*

After the strategy starts, the daemon places the protective OCA pair within
a minute (or whenever the next tick runs).

---

### Workflow 3 — "I want to scale into TSLA on a confirmed bottom"

> *Start a reversal entry on TSLA: total budget $30,000, three weighted tranches (20/30/50), default 3-of-5 threshold, abort after 14 days of stall.*

The daemon watches signals hourly. Tranches fire only when the signal
count has held for the required window.

---

### Workflow 4 — "Tighten my stops on a winning position"

> *Update the AAPL swing strategy: change the trail multiplier to 1.0 and the floor to $10 below cost.*

Within a second, the old OCA pair is cancelled and a new tighter pair is
placed.

---

### Workflow 5 — "Walk away for the weekend"

You don't need to do anything. The daemon runs 24/7, the strategies tick
on their own, fills are detected via events in real-time, and IBKR's
nightly Gateway restart is handled automatically.

If you want a status snapshot before leaving:

> *Give me a full snapshot: connection status, portfolio, regime on AAPL/NVDA/TSLA, status of all running strategies.*

---

## What to do when things look wrong

| Symptom | First thing to ask Claude |
|---|---|
| Strategy says HOLDING but I don't see orders in TWS | *Tick the [symbol] strategy now and show me the result.* |
| Config update didn't seem to take effect | *Show me the swing status for [symbol] — does the live OCA match the config?* |
| Daemon went silent | *Check the IBKR connection status.* |
| Wondering why something didn't fire | *Show me the reversal status for [symbol] — what's the consecutive-days counter?* |

If Claude can't figure it out, fall back to SSH'ing into the VPS:

```bash
journalctl -u ibkr-mcp -n 50 --no-pager      # daemon's recent logs
docker compose -f ~/ibkr-stack/docker-compose.yml logs ib-gateway --tail 30
```

---

## Don't trust this document — confirm in TWS

For real money: every time the daemon claims it placed an order, **verify
in TWS (or Gateway's VNC view) that the order actually appears with the
right parameters.** Software bugs happen. The state file isn't truth; the
broker's view is.
