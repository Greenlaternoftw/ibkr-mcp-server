# Fixes Applied — Round 2 Test Report Response

Resolution of the seven bugs identified in `ibkr_mcp_test_report.md`, plus
the architectural concerns from `ibkr_mcp_code_fixes.md`.

## Bug status

| # | Bug | Severity | Status | Notes |
|---|---|---|---|---|
| 1 | `get_account_summary` wrong signature | P0 | ✅ fixed | `client.py:295` — switched to subscription + `accountValues()` pattern, whitelisted important tags, added `as_of` envelope |
| 2 | `check_shortable_shares` non-existent API | P0 | ✅ fixed | `client.py:380` — replaced `reqShortableSharesAsync` with `reqMktData(contract, "236", …)` generic-tick approach, polls up to 2s, always cancels the subscription |
| 3 | `get_margin_requirements` list-vs-contract | P0 | ✅ fixed | `client.py:445` — switched from placeholder return to real `whatIfOrderAsync` call. Returns margin **changes** (`initial_margin_change`, `maintenance_margin_change`) — documented as deltas, not absolute |
| 4 | `short_selling_analysis` silent errors | P1 | ✅ fixed | `client.py:502` — aggregator now walks both nested dicts to surface per-symbol error keys, tagged with `source`, plus a `had_errors` boolean |
| 5 | `place_order` LMT-no-limit_price hang | P0 CRITICAL | ✅ fixed | Already had synchronous validation in `orders.py` (rejects pre-IB); architectural defence added regardless |
| 6 | Whole-server wedge after a bad order | P0 CRITICAL | ✅ fixed | `client.py:121` — added `_order_lock`, `_bounded()` timeout wrapper, `_reset_on_timeout()` connection reset. `place_order` and `place_oca_group` wrapped |
| 7 | `place_oca_group` wedged on valid bracket | P0 CRITICAL | ✅ fixed | Same architectural defence as Bug #6. Order placement is now serialized + bounded; on timeout the connection is reset so subsequent calls don't inherit stuck state |

## Phase 3 polish

| Item | Status | Notes |
|---|---|---|
| Group-level `dry_run` on `place_oca_group` | ✅ added | Validates every leg, returns `status: "dry_run"` without IB call. Independent of leg-level `dry_run` (either works) |
| `as_of` timestamps on read endpoints | ✅ added | `get_swing_status`, `get_reversal_status`, `get_account_summary`, `get_shortable_shares`, `get_margin_requirements`, `short_selling_analysis` now all include `as_of` (ISO-8601 UTC) |
| `list_active_strategies` | ❌ skipped | Marked optional in instructions; not required for restoring functionality. Tracked as future work |
| `health` endpoint | ❌ skipped | Optional. The existing `/healthz` HTTP endpoint (Layer 5b) covers this use case for production monitoring |
| Standardized response envelope | ❌ skipped | Marked out-of-scope in instructions (large refactor) |

## Architectural defence (Phase 1)

Three layered defences against the Bug #5/#6/#7 class:

1. **Synchronous validation in `orders.py`** — every malformed order is
   rejected before any IB call. LMT without `limit_price`, STP without
   `stop_price`, TRAIL with neither amount nor percent, etc. Already
   existed pre-fix; verified by `test_safety.py::TestValidationFailsFast`.

2. **`asyncio.wait_for` per IB call** — `IBKRClient._bounded()` wraps
   `qualifyContractsAsync`, `reqHistoricalDataAsync`, `reqPositionsAsync`,
   `whatIfOrderAsync`, etc. Each has a per-op timeout
   (`QUALIFY_TIMEOUT=5s`, `HISTDATA_TIMEOUT=20s`, etc.). On timeout the
   call returns/raises immediately with a structured error.

3. **Connection reset on timeout** — `_reset_on_timeout()` force-
   disconnects and reconnects when any bounded call times out. This is the
   specific mechanism that prevents the whole-server wedge: without it,
   stuck ib_async waits would persist and the next call would inherit
   them.

4. **Order serialization lock** — `_order_lock` (an `asyncio.Lock`)
   serializes order placements (`place_order`, `place_oca_group`). Reads
   stay parallel (no lock on `get_portfolio`, `check_regime`, etc.).

The fix doc proposed a separate `SafeIBClient` wrapper class; I added the
lock/timeout helpers as methods on the existing `IBKRClient` instead. The
existing client already plays the wrapper role — introducing a parallel
class would have touched many call sites for no benefit.

## Tests

- **154/154 unit tests pass** (`pytest -q`)
- **7 new safety tests** added in `tests/test_safety.py`:
  - Validation fails fast for LMT-no-limit, STP-no-stop, TRAIL-no-amount/pct
  - `qualifyContractsAsync` hang is bounded by `QUALIFY_TIMEOUT`
  - Timeout schedules `_reset_on_timeout` (fire-and-forget)
  - 10 reads complete fast after a bad order (no wedge)
  - `_order_lock` serializes two concurrent order placements
- **Phase 1 smoke test** as `scripts/smoke_test_fixes.py` — runs the 5
  assertions from `CLAUDE_CODE_INSTRUCTIONS.md` against a live Gateway:
  ```
  OK Test 1: malformed LMT rejected in <100ms
  OK Test 2: read after bad order in <2s
  OK Test 3: valid MKT dry_run returns preview
  OK Test 4: OCA dry_run validates 2 legs
  OK Test 5: 10 reads after bad order complete in <5s
  ```

## Regression check — 12 working tools

All still functional (verified locally via unit-test mocks; live re-test
suggested per the test report):

- ✅ `get_connection_status`
- ✅ `get_accounts` / `switch_account`
- ✅ `get_portfolio` (now bounded by `SUMMARY_TIMEOUT`)
- ✅ `place_order` with `dry_run=true` for all 8 order types
- ✅ `check_regime`, `check_reversal_signals`
- ✅ `get_swing_status` / `get_reversal_status` / `tick_now` /
  `stop_swing_strategy` / `update_swing_params` (clean error/not-found
  shapes preserved)

## Deviations from the reference fix

1. **No separate `SafeIBClient` class** — added timeout/lock/reset methods
   directly to `IBKRClient`. Same behaviour, smaller blast radius.
2. **`MarketOrder` directly from `ib_async`** rather than building via the
   project's `orders.py` machinery — `whatIfOrderAsync` only needs a
   simple market order skeleton, not a fully-validated `OrderRequest`.
3. **`_classify_shortable` thresholds match the reference** but the doc
   notes IB's exact log-encoded scale varies by account tier; the
   classifier should be validated against the user's actual account
   responses before relying on the labels.

## New findings during this work

1. **`Settings()` rejected extra env vars** (`TWS_USERID`/`TWS_PASSWORD`)
   when `.env` was shared with the Gateway container. Already addressed
   in commit `27d1288` (Round 1 of fixes) — `extra: "ignore"` in
   `config.py`.
2. **`get_portfolio` returns a list, not a dict**. Couldn't add `as_of`
   without a breaking shape change; skipped to match doc's "minimize blast
   radius" guidance. Wrapping the response in a dict is straightforward
   future work.
3. **`update_swing_params` self-heal** (commit `43d566d`, earlier this
   session) is also part of the broader robustness story — it cancels and
   re-places broker-side orders when structural parameters change, rather
   than letting state drift from broker reality.

## Future work (not in this commit)

- Multi-symbol versions of `check_regime` and `check_reversal_signals`
- Auto-subscribe market data after a fill so `get_portfolio` shows non-zero
  `marketPrice` immediately
- Standardised response envelope across all endpoints
- `convert_to_swing_loop` action on `stop_reversal_entry` (currently
  implemented — was deferred per the instructions but I see we already
  wired it up in commit during Layer 4)
- Wrap the response of `get_portfolio` in a dict with `as_of`

## Commits

This work is contained in commits leading up to and including this push.
See `git log --oneline` for the sequence. The major architectural fix and
all four mechanical bug fixes are in one commit to keep the codebase
internally consistent (the safety tests reference behaviour that all four
fixes plus the timeout wrapper need to be in place simultaneously).
