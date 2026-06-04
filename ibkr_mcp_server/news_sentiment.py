"""News sentiment via yfinance + local lexicon (Phase F).

The original Phase F implementation used the Anthropic API with the
web_search tool. That worked but burned ~$0.01-0.05 per refresh and
required active Anthropic billing -- when the workspace cap was hit,
sentiment silently degraded to "gate disabled."

This module replaces that with:

  - yfinance Ticker.news for headlines (free, no API key)
  - A small POS/NEG keyword lexicon for sentiment classification
  - 6-hour per-symbol cache so we don't hammer Yahoo

Trade-off: lexicon scoring is less nuanced than Claude's reading of
each headline. Compensated by:
  - The gate's threshold (-5) is conservative; only strong net-negative
    clusters block. Lexicon false positives are rare in that regime.
  - News items are filtered to last 72 hours so noise from old stories
    can't accumulate.
  - The classifier is transparent and tweakable -- if you find it
    misfiring on a real headline, just add/remove keywords here.

If you want LLM-quality scoring back later, swap in a per-headline
Claude call (much cheaper than the web_search loop -- ~$0.001 per
score) but for now the lexicon is good enough.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# {symbol -> (fetched_at, sentiment_dict)}
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

# Only count news from the last N hours -- older items aren't actionable
# for a short-horizon mean-reversion strategy.
_RECENCY_WINDOW_HOURS = 72


# Compact lexicon. Each keyword is a substring match (case-insensitive).
# Magnitude 1-5 based on how directly the word implies the impact.
# Tuned to MINIMIZE false positives: words like "growth", "decline",
# "high", "low" intentionally OMITTED because they're directional in
# headlines but vague (e.g. "stock hits new high" is positive but
# "high inflation" is negative).
_POSITIVE = {
    # earnings / fundamentals
    "beat": 3, "beats": 3, "surpass": 2, "surpasses": 2,
    "outperform": 2, "outperforms": 2, "record": 2, "raises guidance": 4,
    "raised guidance": 4, "raises forecast": 3, "exceed": 2, "exceeds": 2,
    # ratings
    "upgrade": 3, "upgraded": 3, "buy rating": 3, "overweight": 2,
    "outperforms": 2, "price target raise": 3, "price target raised": 3,
    "raised price target": 3, "lifted price target": 3,
    # corporate actions
    "approval": 3, "approved": 3, "fda approves": 4, "fda approval": 4,
    "patent granted": 3, "wins contract": 3, "wins deal": 3,
    "announces dividend": 2, "increases dividend": 3, "raised dividend": 3,
    "stock split": 2, "buyback": 3, "share repurchase": 3,
    # business momentum
    "surge": 3, "surges": 3, "soars": 3, "rallies": 2, "jumps": 2,
    "expansion": 2, "expands": 2, "acquisition": 2, "partnership": 2,
    "strategic deal": 2, "launches": 1,
}

_NEGATIVE = {
    # earnings / fundamentals
    "miss": 3, "misses": 3, "missed": 3, "shortfall": 3,
    "cuts guidance": 4, "cut guidance": 4, "lowers guidance": 4,
    "slashes guidance": 5, "withdraws guidance": 4, "guidance cut": 4,
    "warns": 3, "warning": 3,
    # ratings
    "downgrade": 3, "downgraded": 3, "sell rating": 3, "underweight": 2,
    "price target cut": 3, "price target lowered": 3, "lowered price target": 3,
    # legal / regulatory / risk
    "lawsuit": 3, "sued": 3, "investigation": 4, "probe": 3, "fraud": 5,
    "subpoena": 4, "indicted": 5, "settlement": 2, "fines": 3,
    "recall": 4, "halted": 3, "delisted": 5,
    "fda rejects": 4, "fda rejected": 4, "rejection": 3, "complete response letter": 4,
    # corporate distress
    "bankruptcy": 5, "chapter 11": 5, "layoffs": 3, "layoff": 3,
    "restructuring": 2, "going concern": 5, "covenant": 3,
    "ceo steps down": 3, "cfo resigns": 3, "resigns": 2, "fires": 3,
    # market action (only when applied to the stock itself)
    "plunge": 3, "plunges": 3, "tumbles": 3, "slump": 3, "slumps": 3,
    "crash": 4, "crashes": 4, "sinks": 3, "drops": 1,
}


def _kw_matches(text: str, kw: str) -> bool:
    """Word-boundary match: kw="beat" matches "beat" but NOT "beats".
    Multi-word keywords ("raises guidance") are matched as substrings
    (regex word boundaries don't work cleanly across spaces).
    """
    if " " in kw:
        return kw in text
    return re.search(rf"\b{re.escape(kw)}\b", text) is not None


def _classify_headline(title: str, summary: str = "") -> tuple:
    """Score one headline by word-boundary matching against the lexicon.

    Returns ``(impact, magnitude)`` where impact is
    ``POSITIVE`` / ``NEGATIVE`` / ``NEUTRAL`` and magnitude is 0-5.
    Magnitude is the difference between matched positive and negative
    keyword weights, clamped to [-5, 5].

    Word boundaries prevent the "layoffs" vs "layoff" double-counting
    bug -- the singular form's regex requires a word boundary after
    the 'f', so "layoffs" matches only the plural entry.
    """
    text = (title or "") + " " + (summary or "")
    text = text.lower()
    pos_score = sum(
        weight for kw, weight in _POSITIVE.items() if _kw_matches(text, kw)
    )
    neg_score = sum(
        weight for kw, weight in _NEGATIVE.items() if _kw_matches(text, kw)
    )
    net = pos_score - neg_score
    if net > 0:
        return ("POSITIVE", min(net, 5))
    if net < 0:
        return ("NEGATIVE", min(-net, 5))
    return ("NEUTRAL", 0)


def _score_news_items(news: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure scoring function -- testable in isolation.

    Walks a list of items (whether scored by the lexicon or by an LLM)
    and returns the aggregate stats. Validates magnitude + impact so
    junk items get silently dropped.
    """
    score = 0
    n_pos = n_neg = n_neu = 0
    most_negative = None
    most_negative_mag = 0
    for item in news or []:
        try:
            impact = str(item.get("impact", "")).upper()
            magnitude = int(item.get("magnitude", 0) or 0)
            if magnitude < 1 or magnitude > 5:
                # NEUTRAL items have magnitude 0 -- count them but
                # don't contribute to score.
                if impact == "NEUTRAL":
                    n_neu += 1
                continue
            if impact == "POSITIVE":
                score += magnitude
                n_pos += 1
            elif impact == "NEGATIVE":
                score -= magnitude
                n_neg += 1
                if magnitude > most_negative_mag:
                    most_negative_mag = magnitude
                    most_negative = item.get("headline")
            elif impact == "NEUTRAL":
                n_neu += 1
        except (TypeError, ValueError):
            continue
    return {
        "score": score,
        "n_items": len(news or []),
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_neutral": n_neu,
        "top_negative": most_negative,
    }


def evaluate_sentiment(
    score: Optional[int],
    *,
    block_threshold: int = -5,
) -> Optional[bool]:
    """True = ok / False = blocked. None when score is unavailable."""
    if score is None:
        return None
    return score > block_threshold


def _fetch_yfinance_news(symbol: str, today: dt.datetime) -> List[Dict[str, Any]]:
    """Pull headlines from yfinance, score each with the lexicon, return
    items shaped for `_score_news_items`. Filters to last
    ``_RECENCY_WINDOW_HOURS``.

    Defensive: yfinance's news endpoint shape has shifted across
    releases; we read fields with .get() and skip malformed entries.
    Returns [] on any error (graceful degrade -> gate disabled).
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("news_sentiment: yfinance not installed")
        return []

    try:
        raw = yf.Ticker(symbol).news or []
    except Exception as e:
        logger.warning(f"news_sentiment yfinance fetch failed for {symbol}: {e}")
        return []

    cutoff = today.timestamp() - (_RECENCY_WINDOW_HOURS * 3600)
    out: List[Dict[str, Any]] = []
    for n in raw:
        try:
            # yfinance >= 0.2.50 nests fields under "content"; older
            # versions had them flat. Read defensively from both.
            content = n.get("content") if isinstance(n, dict) else None
            if isinstance(content, dict):
                title = content.get("title") or n.get("title") or ""
                summary = content.get("summary") or content.get("description") or ""
                pub_date = content.get("pubDate")
                # pubDate is ISO string in new format
                if pub_date:
                    try:
                        published = dt.datetime.fromisoformat(
                            pub_date.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        published = None
                else:
                    published = n.get("providerPublishTime")
            else:
                title = n.get("title") or ""
                summary = n.get("summary") or ""
                published = n.get("providerPublishTime")
            if not title:
                continue
            # Age filter
            if published is not None and published < cutoff:
                continue
            impact, magnitude = _classify_headline(title, summary)
            out.append({
                "headline": title[:200],
                "impact": impact,
                "magnitude": magnitude,
                "rationale": "lexicon-scored from yfinance headline",
            })
        except Exception:
            continue
    # Cap to top-10 most recent (yfinance returns ~10-20 typically).
    return out[:10]


async def get_news_sentiment(
    symbol: str,
    *,
    today: Optional[dt.datetime] = None,
    force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fetch + cache news sentiment for ``symbol``.

    Pipeline:
      1. Pull last ~10 headlines from yfinance
      2. Filter to last 72 hours
      3. Score each with the lexicon
      4. Aggregate via `_score_news_items`
      5. Apply the block-threshold rule via `evaluate_sentiment`
      6. Cache the result for 6 hours

    Returns dict shaped:
      ``{score, sentiment_ok, n_items, n_positive, n_negative,
         n_neutral, top_negative, fetched_at, items}``
    Or None on hard failure (yfinance ImportError, or no news at all).

    The function is async to keep the calling signature stable -- the
    yfinance call itself is sync (runs in the asyncio default executor
    via the to_thread wrapper to keep the event loop responsive).
    """
    import asyncio as _asyncio
    now = today or dt.datetime.now(dt.timezone.utc)
    symbol = symbol.upper().strip()

    cached = _CACHE.get(symbol)
    if (not force_refresh) and cached:
        fetched_at, payload = cached
        age = (now - fetched_at).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            return payload

    # yfinance is synchronous + slow (~1-3s). Run in thread so we don't
    # block the event loop / starve the pivot tick scheduler.
    try:
        items = await _asyncio.to_thread(_fetch_yfinance_news, symbol, now)
    except Exception as e:
        logger.warning(f"news_sentiment {symbol}: thread fetch failed: {e}")
        return None

    if not items:
        # No fresh news. Treat as neutral (gate disabled rather than
        # erroneously blocking) by caching an empty result.
        payload = {
            "score": 0,
            "n_items": 0,
            "n_positive": 0,
            "n_negative": 0,
            "n_neutral": 0,
            "top_negative": None,
            "sentiment_ok": True,  # no news = no reason to block
            "fetched_at": now.isoformat(),
            "items": [],
        }
        _CACHE[symbol] = (now, payload)
        return payload

    stats = _score_news_items(items)
    payload = {
        **stats,
        "sentiment_ok": evaluate_sentiment(stats["score"]),
        "fetched_at": now.isoformat(),
        "items": items,
    }
    _CACHE[symbol] = (now, payload)
    return payload


def clear_cache() -> None:
    """Test helper / future force-refresh button."""
    _CACHE.clear()
