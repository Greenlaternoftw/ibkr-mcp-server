"""Free price fallback via yfinance.

IBKR real-time/delayed quotes require a market-data subscription. When the
account doesn't have one (quotes come back NaN), positions render at $0
and unrealized P&L can't be computed. This module provides a FREE backup
price source (yfinance) so the dashboard still shows accurate-ish prices.

IBKR is always tried FIRST (in the positions route); this is only used to
fill symbols IBKR couldn't price. Prices here are typically ~15 min
delayed (Yahoo), which is fine for a portfolio display -- and they're
tagged so the UI marks them as a fallback, never mistaken for live.

Defensive: yfinance's surface shifts across versions and it scrapes, so
every lookup is wrapped; a miss yields no price rather than an error.
"""

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


def _one_price(tk) -> float:
    """Best-effort last price from a yfinance Ticker, across versions."""
    # fast_info is the cheap path (no full quote scrape).
    try:
        fi = tk.fast_info
        for key in ("last_price", "lastPrice"):
            try:
                v = fi[key] if key in fi else getattr(fi, key, None)
            except Exception:
                v = None
            if v and v == v and float(v) > 0:   # not None, not NaN, positive
                return float(v)
        # previous close as a last resort (market closed / thin).
        for key in ("previous_close", "previousClose"):
            try:
                v = fi[key] if key in fi else getattr(fi, key, None)
            except Exception:
                v = None
            if v and v == v and float(v) > 0:
                return float(v)
    except Exception:
        pass
    # Fall back to a 1-day history close.
    try:
        h = tk.history(period="1d")
        if h is not None and not h.empty:
            c = float(h["Close"].iloc[-1])
            if c == c and c > 0:
                return c
    except Exception:
        pass
    return 0.0


def fetch_prices(symbols: List[str]) -> Dict[str, float]:
    """Return {symbol: last_price} for as many symbols as yfinance can
    price. Symbols it can't price are omitted (caller keeps IBKR's 0).

    Synchronous + blocking (yfinance) -- call via asyncio.to_thread from
    the async positions route.
    """
    out: Dict[str, float] = {}
    if not symbols:
        return out
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("price_fallback: yfinance not installed")
        return out
    for sym in symbols:
        try:
            px = _one_price(yf.Ticker(sym))
            if px > 0:
                out[sym] = px
        except Exception as e:
            logger.debug(f"price_fallback miss for {sym}: {e}")
    return out
