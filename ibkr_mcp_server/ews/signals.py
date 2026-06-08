"""EWS signal fetchers — the six early-warning signal categories.

Maps the brief's Section 2.1 signals onto concrete data sources:

  1. SEC EDGAR filings (S-1 / 8-K / Form 4)  -- FREE, efts.sec.gov + data.sec.gov
  2. Lock-up & IPO calendar                  -- Unusual Whales /api/calendar/ipo (optional)
  3. Unusual options activity                -- Unusual Whales flow-alerts (optional)
  4. News & conference activity              -- yfinance lexicon (reuses news_sentiment, FREE)
  5. Short-interest spike                    -- Unusual Whales short interest (optional)
  6. Dark-pool positioning                   -- Unusual Whales dark pool (optional)

Design rules (mirroring the brief's "must never break the loop"):
  - Every fetch is wrapped; on any failure it returns an empty structure.
  - No Unusual Whales key  -> UW-backed signals return empty; EDGAR +
    news still populate, so the AI still gets real free signal.
  - All network calls are bounded by a short timeout.
  - SEC requires a descriptive User-Agent with a contact email; we send
    settings.ews_edgar_user_agent and honor their 10 req/s cap by
    serializing EDGAR calls behind a small async lock.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# SEC asks API consumers to stay <= 10 req/s and identify themselves.
# A process-wide lock + tiny spacing keeps us comfortably under that.
_EDGAR_LOCK = asyncio.Lock()
_EDGAR_MIN_SPACING_S = 0.15

_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


# ----------------------------------------------------------------------
# Signal 1 + 2: SEC EDGAR (free) — S-1 / 8-K / Form 4
# ----------------------------------------------------------------------

async def fetch_edgar_filings(symbol: str, *, lookback_days: int = 14) -> Dict[str, Any]:
    """Recent priority filings for a ticker from SEC EDGAR full-text search.

    Returns: {"filings": [{form, filed, title, url}], "has_s1": bool,
              "has_8k": bool, "has_form4": bool, "error": str|None}

    Uses efts.sec.gov full-text search (no key). S-1 = secondary offering
    risk (the AVEX case), 8-K = material event, Form 4 = insider txn.
    """
    out: Dict[str, Any] = {
        "filings": [], "has_s1": False, "has_8k": False,
        "has_form4": False, "error": None,
    }
    headers = {"User-Agent": settings.ews_edgar_user_agent,
               "Accept": "application/json"}
    # EDGAR full-text search covers 2001+; forms filter narrows to the
    # high-signal set. We sort newest-first and keep the lookback window.
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {
        "q": f'"{symbol}"',
        "forms": "S-1,8-K,4",
        "dateRange": "custom",
    }
    try:
        async with _EDGAR_LOCK:
            await asyncio.sleep(_EDGAR_MIN_SPACING_S)
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                # The public full-text endpoint is efts.sec.gov/LATEST/search-index
                r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            out["error"] = f"edgar http {r.status_code}"
            return out
        data = r.json()
        hits = (((data or {}).get("hits") or {}).get("hits")) or []
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=lookback_days)).date()
        for h in hits[:25]:
            src = h.get("_source") or {}
            form = (src.get("file_type") or src.get("root_form") or "").upper()
            filed_raw = (src.get("file_date") or "")[:10]
            try:
                filed = dt.date.fromisoformat(filed_raw)
            except Exception:
                filed = None
            if filed and filed < cutoff:
                continue
            # The accession + cik build a human URL.
            adsh = (src.get("adsh") or "").replace("-", "")
            ciks = src.get("ciks") or []
            cik = (ciks[0] if ciks else "").lstrip("0")
            url_h = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}"
                if cik and adsh else "https://www.sec.gov/cgi-bin/browse-edgar"
            )
            entry = {
                "form": form,
                "filed": filed_raw,
                "title": (src.get("display_names") or [symbol])[0],
                "url": url_h,
            }
            out["filings"].append(entry)
            if form.startswith("S-1"):
                out["has_s1"] = True
            elif form.startswith("8-K"):
                out["has_8k"] = True
            elif form == "4":
                out["has_form4"] = True
    except Exception as e:
        logger.debug(f"edgar fetch failed for {symbol}: {e}")
        out["error"] = str(e)
    return out


# ----------------------------------------------------------------------
# Signals 2,3,5,6: Unusual Whales (optional — needs uw_api_key)
# ----------------------------------------------------------------------

def _uw_enabled() -> bool:
    return bool(settings.uw_api_key)


async def _uw_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """Single Unusual Whales GET. Returns parsed JSON or None on any issue
    (no key, http error, timeout). Never raises."""
    if not _uw_enabled():
        return None
    url = settings.uw_base_url.rstrip("/") + path
    headers = {
        "Authorization": f"Bearer {settings.uw_api_key}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(url, params=params or {}, headers=headers)
        if r.status_code != 200:
            logger.debug(f"UW {path} -> http {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"UW {path} failed: {e}")
        return None


async def fetch_uw_signals(symbol: str) -> Dict[str, Any]:
    """The four Unusual Whales-backed signals, fetched concurrently.

    Returns {"flow_alerts", "short", "dark_prints", "ipo", "available"}.
    available=False means no UW key configured (free-tier operation).
    """
    out: Dict[str, Any] = {
        "flow_alerts": [], "short": {}, "dark_prints": [],
        "ipo": [], "available": _uw_enabled(),
    }
    if not _uw_enabled():
        return out

    flow_t = _uw_get("/api/option-trades/flow-alerts",
                     {"ticker": symbol, "limit": 15})
    short_t = _uw_get(f"/api/shorts/{symbol}/interest-float/v2")
    dark_t = _uw_get(f"/api/darkpool/{symbol}", {"limit": 20})
    ipo_t = _uw_get("/api/calendar/ipo")

    flow, short, dark, ipo = await asyncio.gather(
        flow_t, short_t, dark_t, ipo_t, return_exceptions=True
    )

    def _data(x):
        if isinstance(x, Exception) or x is None:
            return None
        # UW commonly wraps payloads in {"data": [...]}.
        return x.get("data", x) if isinstance(x, dict) else x

    fd = _data(flow)
    if isinstance(fd, list):
        out["flow_alerts"] = fd[:15]
    sd = _data(short)
    if isinstance(sd, (dict, list)):
        out["short"] = sd
    dd = _data(dark)
    if isinstance(dd, list):
        out["dark_prints"] = dd[:20]
    ipod = _data(ipo)
    if isinstance(ipod, list):
        # Keep only IPO/lock-up rows mentioning this ticker.
        out["ipo"] = [row for row in ipod
                      if isinstance(row, dict)
                      and str(row.get("ticker", "")).upper() == symbol.upper()]
    return out


# ----------------------------------------------------------------------
# Signal 4: News (free — reuses the yfinance lexicon)
# ----------------------------------------------------------------------

async def fetch_news_signal(symbol: str) -> Dict[str, Any]:
    """News headlines + lexicon sentiment, reusing news_sentiment.

    news_sentiment.evaluate_sentiment is sync (yfinance), so we run it in
    a thread to avoid blocking the event loop.
    """
    try:
        from .. import news_sentiment
        result = await asyncio.to_thread(news_sentiment.evaluate_sentiment, symbol)
        return result or {}
    except Exception as e:
        logger.debug(f"news signal failed for {symbol}: {e}")
        return {}


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------

async def fetch_all_signals(symbol: str) -> Dict[str, Any]:
    """Fetch all six signal categories for a ticker concurrently.

    Returns a single structured dict the AI analyzer consumes. Every
    sub-fetch is independently failure-isolated, so a dead source yields
    an empty section rather than taking the whole scan down.
    """
    edgar_t = fetch_edgar_filings(symbol)
    uw_t = fetch_uw_signals(symbol)
    news_t = fetch_news_signal(symbol)

    edgar, uw, news = await asyncio.gather(
        edgar_t, uw_t, news_t, return_exceptions=True
    )

    def _safe(x, default):
        return default if isinstance(x, Exception) or x is None else x

    return {
        "symbol": symbol,
        "edgar": _safe(edgar, {"filings": [], "error": "exception"}),
        "uw": _safe(uw, {"available": _uw_enabled()}),
        "news": _safe(news, {}),
        "uw_available": _uw_enabled(),
    }
