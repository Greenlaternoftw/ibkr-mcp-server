"""Forward-looking implied volatility lookup via IBKR option chain.

Phase D2: real IV beats the realized-vol proxy for forward-looking
event detection. Compute IV from the ATM call's modelGreeks.

  - reqSecDefOptParams for the underlying → expirations + strikes
  - Pick the expiry closest to ~30 days out
  - Pick the strike closest to current underlying price (ATM)
  - reqMktData with generic tick "106" for model greeks → IV
  - Multiply by 100 to get IV30 as a percentage

Caching: 1h per symbol. IV doesn't move minute-to-minute and option
chain queries hit IBKR's pacing limits if hammered.

Fail-safe: every error path returns None. Caller falls back to the
realized-vol proxy (Phase D), which is the existing behavior. So
turning on this module is purely additive -- if it works, the loop
gets better signal; if it doesn't, nothing changes vs the previous
shipped behavior.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# {symbol -> (fetched_at, iv30_pct)}
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 3600  # 1 hour
_TARGET_DTE_DAYS = 30
_TICKER_SNAPSHOT_TIMEOUT = 5.0


async def get_iv30_pct(
    client,
    symbol: str,
    *,
    force_refresh: bool = False,
    today: Optional[dt.datetime] = None,
) -> Optional[float]:
    """Return IV30 (annualized %, e.g. 22.4) for `symbol`, or None on
    failure / cache miss with no live data.

    `client` is an IBKRClient instance; typed as Any to avoid the
    circular import.

    Cached for 1 hour. Returns the cached value on hit regardless of
    `force_refresh=False`.
    """
    now = today or dt.datetime.now(dt.timezone.utc)
    symbol = symbol.upper().strip()
    cached = _CACHE.get(symbol)
    if (not force_refresh) and cached:
        fetched_at, iv = cached
        if (now - fetched_at).total_seconds() < _CACHE_TTL_SECONDS:
            return iv

    try:
        iv = await _fetch_iv30_from_ibkr(client, symbol)
    except Exception as e:
        logger.warning(f"iv30 fetch failed for {symbol}: {e}")
        iv = None

    _CACHE[symbol] = (now, iv)
    return iv


async def _fetch_iv30_from_ibkr(client, symbol: str) -> Optional[float]:
    """The actual IBKR roundtrip. Broken out so the caller's cache
    + error handling stays simple."""
    from ib_async import Stock, Option

    ib = client.ib

    # 1. Get the current underlying mark so we can find the ATM strike.
    try:
        last = await client.get_market_data(symbol)
        underlying_price = float(last.get("last") or last.get("close") or 0)
    except Exception:
        underlying_price = 0.0
    if underlying_price <= 0:
        logger.debug(f"iv30 {symbol}: no underlying price")
        return None

    # 2. Pull option-chain parameters (expirations + strikes).
    underlying = Stock(symbol, "SMART", "USD")
    await ib.qualifyContractsAsync(underlying)
    if not getattr(underlying, "conId", 0):
        return None

    chains = await ib.reqSecDefOptParamsAsync(
        underlyingSymbol=symbol,
        futFopExchange="",
        underlyingSecType="STK",
        underlyingConId=underlying.conId,
    )
    # Prefer SMART exchange entries.
    chain = next((c for c in chains if c.exchange == "SMART"), None)
    if not chain or not chain.expirations or not chain.strikes:
        logger.debug(f"iv30 {symbol}: no usable option chain")
        return None

    # 3. Pick expiry closest to +30 days from today.
    today = dt.date.today()
    target = today + dt.timedelta(days=_TARGET_DTE_DAYS)
    expirations = sorted(chain.expirations)
    best_exp = min(
        expirations,
        key=lambda e: abs((dt.datetime.strptime(e, "%Y%m%d").date() - target).days),
    )

    # 4. Pick ATM strike (closest to underlying price).
    strikes = sorted(chain.strikes)
    atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))

    # 5. Build the ATM call contract, qualify, request market data with
    #    generic tick "106" (model option computation = greeks + IV).
    opt = Option(symbol, best_exp, atm_strike, "C", "SMART")
    await ib.qualifyContractsAsync(opt)
    if not getattr(opt, "conId", 0):
        return None

    ticker = ib.reqMktData(opt, "106", False, False)
    try:
        # Wait briefly for modelGreeks to populate. We don't need
        # tick-perfect timing; modelGreeks updates within a second
        # of subscription on liquid names.
        iv_pct: Optional[float] = None
        deadline = asyncio.get_event_loop().time() + _TICKER_SNAPSHOT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            mg = getattr(ticker, "modelGreeks", None)
            if mg and getattr(mg, "impliedVol", None):
                iv_pct = float(mg.impliedVol) * 100.0
                break
            await asyncio.sleep(0.3)
    finally:
        try:
            ib.cancelMktData(opt)
        except Exception:
            pass
    return iv_pct


def clear_cache() -> None:
    """Test helper / future force-refresh button."""
    _CACHE.clear()
