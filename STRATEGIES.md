# Strategy design rationale

Why each layer is designed the way it is. Operator-level theory — what the
strategies are trying to do, and why they're constructed in specific ways
rather than alternatives.

## The mental model

The whole stack is built around a single thesis: **in any given market
session, three distinct regimes exist, and a strategy that performs well
in one performs poorly in the other two.** The three regimes are:

1. **Calm uptrend.** Price rising, low volatility, no parabolic ADX-spiking
   move. This is where buy-the-dip is profitable: small dips are buying
   opportunities, the trend reasserts itself within days, and protective
   stops are rarely hit.

2. **Drawdown / downtrend.** Price falling, volatility elevated, ADX
   often high. Buy-the-dip here is catching falling knives. Protective
   stops fire repeatedly. Cost basis drifts down faster than dip-buys
   can recover it.

3. **Bottom / reversal.** Price has fallen and is bouncing. Multiple
   technical signals (RSI divergence, MACD crossover, higher swing-low,
   etc.) flash buy, but most of the time these are false positives —
   the price keeps falling for another leg.

The system's job is **(a)** recognize which regime we're in, **(b)** only
buy aggressively during (1), **(c)** carefully scale in during (3) when
multiple signals align, **(d)** stop out cleanly when (2) takes over.

Each of the 6 layers handles one piece of this.

## Layer 2 — the regime filter (why three gates, why those three)

The filter outputs `enabled=true` only when all three are simultaneously
satisfied:

### Gate 1 — SMA(50) rising

> SMA(50) today > SMA(50) five days ago

The simplest possible trend gate. We don't care about absolute direction
or slope magnitude — only that the 50-day moving average is up over a
5-day window. This filters out clear downtrends without being so strict
that gentle consolidations fail.

**Why 50 and 5?** Day-trading systems often use SMA(20) for "trend";
position trading uses SMA(200). 50 is the standard swing-trading horizon,
matching the typical hold-time of the swing loop. The 5-day comparison
window is short enough to react within a week to genuine trend changes
but long enough to ignore single-day noise.

### Gate 2 — ADX(14) below threshold

> ADX(14) < 25 (default)

ADX measures **trend strength**, not direction. Counterintuitively, we
want **low** ADX, not high — because:

- A high-ADX uptrend (ADX > 40) is a parabolic move. Parabolic moves
  end badly. The pullback when they break is fast and deep.
- A high-ADX downtrend (ADX > 40) is what we explicitly don't want
  to be buying into.
- A low-ADX regime (ADX < 25) means the market is moving but not in
  a hurry. This is the calm-uptrend territory where dip-buying works.

**Why 25?** It's the conventional ADX threshold for "trending vs.
ranging" — anything above is "trending strongly." We want trending
just enough to make money, not so much that we're chasing.

### Gate 3 — ATR%(14) below its 100-day average

> ATR(14) today / close < average(ATR%(14) over last 100 days)

ATR% (Average True Range as a percentage of close) measures realized
**volatility**. The gate passes when recent volatility is below its
trailing 100-day average — i.e., things are calm right now relative
to the recent regime.

**Why a percentage and not absolute ATR?** A $5 ATR on a $50 stock and a
$50 ATR on a $500 stock are entirely different volatility regimes. The
percentage normalizes.

**Why 100-day average?** Long enough to span a typical earnings cycle,
short enough to update meaningfully as regime changes. 50 is too
reactive; 200 is too sluggish.

### Why all three must pass (default)

Each gate alone produces too many false positives:

- Gate 1 alone: passes during high-vol slow grinds upward → ATR% gate
  filters those out.
- Gate 2 alone: passes during sideways chop → trend gate filters out.
- Gate 3 alone: passes during calm-but-already-falling moves → trend
  gate catches that.

Three independent gates with the same direction of intent ≈ 8× harder
to pass by chance than a single gate. This is the same logic as the
3-of-5 confluence in Layer 3 (below).

### Anti-whipsaw smoothing (3 consecutive days)

Without smoothing, the filter flips on and off as single days cross
each threshold. That would cause the trading layers to alternate
between "regime enabled, dip-buy" and "regime disabled, cancel" each
day, wearing capital down through transaction costs.

The smoothing requires 3 consecutive same-direction readings before
`sticky_enabled` or `sticky_disabled` flips. Three days = roughly a
trading week's worth of confirmation. This deliberately lags real
transitions, accepting that the system will be slow to act on regime
changes in exchange for not getting whipsawed.

## Layer 3 — reversal entry (why 5 signals, why 3-of-5 threshold)

A bottom in the market is the most-anticipated and most-disagreed-on
event in technical analysis. Single indicators flash buy at false
bottoms 60-70% of the time. The math of confluence:

If each indicator has 35% true-positive rate at calling actual reversal
days, then requiring 3 of 5 to fire on the same day gives roughly:

```
P(at least 3 of 5 fire on a true bottom) ≈ 35-50% (calibration-dependent)
P(at least 3 of 5 fire on a false bottom) ≈ 5-15%
```

The exact numbers depend on the correlations between signals (which are
positive — they all measure related things), but the principle holds:
requiring confluence drops the false-positive rate dramatically while
only modestly reducing true positives.

### The five signals (why these five specifically)

| Signal | What it captures |
|---|---|
| Bullish RSI divergence | Selling exhaustion: price hits new low, momentum doesn't |
| RSI cross above 30 | Oversold-to-recovery: the actual transition out of capitulation |
| MACD bullish crossover | Trend reversal at the shorter timeframe (12/26 EMA) |
| Higher swing-low | Confirmation: the bottom is now visible in the structure |
| Volume surge on up-day | Real money is buying, not just shorts covering |

These five are chosen specifically because they measure **different
dimensions** of a reversal: momentum (RSI), trend (MACD), price
structure (swing pivots), and order flow (volume). If they were all
RSI-based, requiring confluence would just be redundancy. As designed,
each signal is partially independent — the confluence math actually
applies.

### Tranched entry (why scaling in beats lump-sum)

Even when 3 signals fire, the bottom isn't certain. The system places
**Tranche 1 (33%)** at 3 signals, **Tranche 2 (33%)** at 4 signals,
**Tranche 3 (34%)** at 5 signals.

This means:
- A false bottom that draws only 3 signals costs you 33% position size
  before the stop-and-wait protective stop fires.
- A true bottom that draws all 5 signals gets you in at gradually
  better prices than a lump-sum at the 3-signal moment.
- The expected-value math is: small bets when confidence is moderate,
  bigger bets when confidence rises.

This is a Kelly-criterion-style argument. The optimal bet size given
your edge is proportional to the edge. If 3-signal confluence has a
30% edge and 5-signal has a 60% edge, you should bet roughly 2× as
much at 5-signal vs 3-signal. The 33/33/34 default approximates this
without trying to be too clever — `tranche_sizing="weighted"` (20/30/50)
expresses the bigger-bet-at-higher-confidence intuition more
aggressively.

### Stop-and-wait (why this matters)

If 3 signals fire and Tranche 1 fills, but then the signal count drops
back below 3, the system **stops adding tranches and places a protective
stop at `entry_price - 2 × ATR(14)`.** It does NOT liquidate the filled
tranche.

The rationale: a false bottom that pulled 3 signals once might still
recover. But it might also be the first leg of a deeper drawdown. The
2 × ATR stop gives the position room to breathe (most legitimate
reversal pullbacks stay within 2 ATR) while capping downside if it
turns into a real downtrend continuation.

When signals return to 3+ (and have held for 3 days), the system
re-arms: cancels the protective stop and resumes tranching from where
it stopped.

## Layer 4 — swing loop (why ATR-based, why dip re-entry)

Once you hold a position, the swing loop's job is to **let winners run,
cut losers fast, re-enter on dips.** Every piece of that has a reason.

### Why ATR-based trailing stops (not fixed dollar / percent)

A $2 trailing stop on AAPL ($290) is too tight (about 0.7% — gets
stopped by intraday noise). A 5% trailing stop on AAPL is too loose
(about $14.50 — surrenders 5% of your gains on every pullback).

ATR-based stops adapt to the stock's recent volatility:
- A calm AAPL with ATR(14) = $4 gets a 2 × ATR = $8 trail (about 2.8%).
- A volatile NVDA with ATR(14) = $12 gets a $24 trail (about 4%).
- A stock in the middle of an earnings spike with ATR(14) = $25 gets
  a $50 trail (about 8%).

The same `trail_atr_multiplier=2.0` gives appropriate behavior across
the volatility spectrum. Fixed dollar/percent values don't.

### Why the OCA pair (trailing SELL + hard floor STP)

The trailing SELL captures upside extension: it follows price up and
fires when price drops by `trail_amount` from the most recent high.

The hard floor STP at `cost_basis - floor_offset` is the
**absolute-loss guard**: even if the trailing SELL somehow doesn't
fire (gap down, market closed, etc.), the floor stop ensures the
position never bleeds more than `floor_offset` below cost basis.

They're submitted as an OCA group so filling one cancels the other —
IBKR's broker-side guarantee that you don't double-fill (which would
leave you accidentally short).

### Why the 24-hour cooldown

After any fill (sell or buy), the loop refuses to place an opposite-
direction order for 24 hours. This is the system's primary defense
against:

- **Whipsaw**: stop fires at 10:30 AM, price reverses by 10:45, system
  immediately re-enters, gets stopped again at 11:00. Repeat.
- **Stupid behavior in fast markets**: e.g., flash-crash recoveries
  where the prudent action is to wait and see, not to immediately
  re-engage.

24 hours is conservative. If your strategy genuinely needs to react
faster, you can override via `cooldown_hours`, but the system's bias
is "wait at least a day after every fill."

### Why dip re-entry at a fixed offset from the sell price

When the protective trail fires, the system queues a LMT BUY at
`sell_price × (1 - dip_pct)` or `sell_price - dip_amount`. This is
"buy back the dip" — assume the trail-fire was a temporary pullback,
not a regime change.

The regime filter governs whether to actually queue the dip-buy:
- If `regime_filter_enabled=true` and regime is disabled, no
  dip-buy is queued.
- If regime is enabled, the dip-buy is queued and waits for price
  to come down to the limit.

This is the "swing trader's discipline" in code: don't fight the
regime, but inside a friendly regime, buy your stocks back when they
dip.

## Layer 5 — daemon (why event-driven, why reconciliation)

A trading daemon has three failure modes:

1. **Process death.** Crash, OS reboot, OOM kill. Solution: systemd
   restart + state on disk + recovery on startup.
2. **Stale state.** Your in-memory state says "I have an OCA pair
   active" but IBKR cancelled it overnight for reasons. Solution:
   reconcile against IBKR's reality on startup; prefer reality on
   conflict.
3. **Delayed reaction.** Your protective stop fires at 10:30 AM but
   your daemon's next tick isn't until 11:30 AM — you don't know
   you're FLAT for an hour, during which the price could move
   significantly. Solution: subscribe to `ib.execDetailsEvent` so
   fills trigger an immediate tick.

Layer 5a addresses all three. Reconnect-on-disconnect is the fourth
failure mode, which IBC + the gnzsnz Gateway image handle automatically
for Gateway side; the daemon's `_on_disconnect` handler schedules a
reconnect with the existing `@retry_on_failure` decorator on the
client side.

### Why bearer-token auth (Layer 5b)

The MCP HTTP endpoint is the kind of thing that, if exposed to the
public internet without authentication, will be discovered by
opportunistic scanners within hours. The token isn't a serious
defense — it's a backstop in case the firewall/networking layer
fails. The primary defense is "bind to localhost or to a private
overlay like Tailscale."

The daemon **refuses to start** if `MCP_BIND_HOST` is non-localhost
without a token set. This is a deliberate fail-closed check.

## Why no backtest

Several reasons we don't ship one:

1. **Backtests lie about transaction costs.** Real IBKR commissions,
   SEC fees, exchange fees, and slippage typically eat 20-40% of the
   notional alpha of a swing-trading strategy. Most amateur backtests
   model 0.05% slippage and call it a day; reality is more.

2. **Backtests lie about fills.** The trail-SELL in the swing loop
   fires when price drops by `trail_amount` from the recent high.
   "Recent high" is computed differently by every backtester; few
   match how IBKR actually evaluates it server-side.

3. **The regime filter is not directly backtestable in this form.**
   It's a yes/no gate, not a return-generating signal. A "regime
   filter backtest" would measure something like "what was the average
   return during the next N days when regime was enabled vs.
   disabled." That's interesting research, but it's not a backtest
   of the trading system as a whole.

4. **Walk-forward is the honest equivalent.** Run the daemon in
   paper for 4 weeks. The numbers you see are real — same fees,
   same fills, same regime detection. That's worth more than any
   simulated backtest.

If you do want to backtest specific pieces, the signal-detection
functions in `regime.py` and `reversal.py` are pure functions on
OHLCV bars. Feed them historical data, count signals, measure
forward-N-day returns. The system doesn't include this scaffolding;
you'd build it.
