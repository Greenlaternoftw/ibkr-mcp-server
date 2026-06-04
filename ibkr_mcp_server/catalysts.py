"""Catalyst calendar -- earnings, dividends, splits for a symbol.

IBKR doesn't expose a corporate-actions calendar through ib_async (their
``reqFundamentalData`` requires a paid subscription). yfinance is the
pragmatic free path: well-maintained scraper of Yahoo Finance, no API
key, ships the data we actually need (next earnings date, ex-dividend).

Per-symbol per-day cache so we don't hit Yahoo more than once per day
per ticker -- yfinance is slow (~1-3s per cold lookup) and Yahoo rate-
limits aggressive callers.

A failed fetch (network, parse error, symbol not found) returns an
empty list -- the operator still gets pivot analysis, just without
catalyst awareness for that symbol.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# {symbol -> (fetched_on_date, [catalyst, ...])}
_cache: Dict[str, tuple] = {}


def _coerce_date(v) -> "dt.date | None":
    """yfinance returns dates as datetime, date, Timestamp, or string. Normalize."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    # pandas.Timestamp duck-types as datetime; the isinstance above handles it.
    # String fallback: try ISO parse.
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v[:10])
        except Exception:
            return None
    # Anything else (numpy.datetime64 etc) -- try the .date() / pd.to_datetime path.
    try:
        import pandas as pd
        return pd.to_datetime(v).date()
    except Exception:
        return None


def get_upcoming_catalysts(
    symbol: str,
    horizon_days: int = 60,
    *,
    today: "dt.date | None" = None,
) -> List[Dict[str, Any]]:
    """Return upcoming catalysts within ``horizon_days``.

    Returns a list of dicts shaped:
      ``{type, date, days_away, description}``
    where ``type`` is one of ``"earnings"`` / ``"ex_dividend"`` /
    ``"split"``, ``date`` is an ISO date string, and ``days_away`` is
    integer days from today (always >= 0 in the returned list).

    Args:
      symbol: ticker symbol, case-insensitive.
      horizon_days: only include catalysts within this many days from
        today (default 60 -- covers the next quarter's earnings).
      today: override "today" for testability; defaults to ``dt.date.today()``.

    Returns: list (possibly empty). Empty on any fetch error -- check
    server logs for the reason.
    """
    today = today or dt.date.today()
    symbol = symbol.upper().strip()

    cached = _cache.get(symbol)
    if cached and cached[0] == today:
        return cached[1]

    out: List[Dict[str, Any]] = []
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)

        # 1. Earnings date(s). ticker.calendar usually has one entry but
        #    yfinance sometimes returns a list of two (covers a date range).
        try:
            cal = ticker.calendar
        except Exception as e:
            cal = None
            logger.debug(f"yfinance calendar fetch failed for {symbol}: {e}")
        if cal:
            edates = cal.get("Earnings Date") if hasattr(cal, "get") else None
            for ed in (edates if isinstance(edates, list) else [edates] if edates else []):
                d = _coerce_date(ed)
                if d:
                    days_away = (d - today).days
                    if 0 <= days_away <= horizon_days:
                        out.append({
                            "type": "earnings",
                            "date": d.isoformat(),
                            "days_away": days_away,
                            "description": "Quarterly earnings call",
                        })

        # 2. Ex-dividend.
        if cal:
            exdiv = cal.get("Ex-Dividend Date") if hasattr(cal, "get") else None
            d = _coerce_date(exdiv)
            if d:
                days_away = (d - today).days
                if 0 <= days_away <= horizon_days:
                    out.append({
                        "type": "ex_dividend",
                        "date": d.isoformat(),
                        "days_away": days_away,
                        "description": (
                            "Stock drops by dividend amount; not usually "
                            "a thesis-killer but worth knowing"
                        ),
                    })

        # 3. Splits. ticker.splits is a Series of historical splits;
        #    upcoming splits would show in `ticker.actions` -- but
        #    Yahoo's coverage of forward splits is unreliable. Skip
        #    for now; can add if operator asks.

    except ImportError:
        logger.warning("yfinance not installed -- catalysts unavailable")
        return []  # don't cache the ImportError; might be installed later
    except Exception as e:
        # yfinance throws all kinds of weird scraping errors. Log + swallow.
        logger.warning(f"catalyst fetch failed for {symbol}: {e}")

    out.sort(key=lambda c: c["days_away"])
    _cache[symbol] = (today, out)
    return out


def clear_cache() -> None:
    """Wipe the in-process cache. Call at daemon shutdown / for tests."""
    _cache.clear()
