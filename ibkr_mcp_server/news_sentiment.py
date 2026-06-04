"""News sentiment fetcher (Phase F).

Re-uses the existing /chat/api/research/{symbol} pipeline (Claude
web-search + structured JSON parse) to score news sentiment for a
symbol. Per-symbol cache with a 6-hour TTL because:

  - Each research call costs ~$0.01-0.05 in Anthropic credits
  - News doesn't move minute-to-minute; 6h cadence captures real
    intra-day shifts without burning budget
  - The pivot engine ticks every 60s; uncached would mean ~$50/day
    per active loop just for news sentiment

The sentiment score is the weighted sum of news items:
  score = sum( sign(item.impact) × item.magnitude )
        for impact in (POSITIVE=+1, NEUTRAL=0, NEGATIVE=-1)

A negative sentiment exceeding -5 (multiple negative-magnitude-3
items, e.g. a downgrade + a fundamental concern + an industry
headwind) blocks new entries.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# {symbol -> (fetched_at, sentiment_dict)}
_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours


def _score_news_items(news: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure scoring function -- testable in isolation.

    Returns dict shaped:
      {score, n_items, n_positive, n_negative, n_neutral, top_negative}
    where top_negative is the most-impactful negative headline (or None).
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
    score: int,
    *,
    block_threshold: int = -5,
) -> Optional[bool]:
    """True = ok / False = blocked.  None when score is unavailable."""
    if score is None:
        return None
    return score > block_threshold


async def get_news_sentiment(
    symbol: str,
    *,
    today: Optional[dt.datetime] = None,
    force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Fetch + cache news sentiment for `symbol`. Returns dict:
      {score, sentiment_ok, n_items, n_positive, n_negative, n_neutral,
       top_negative, fetched_at}
    Or None on fetch failure (caller skips the gate).

    Cached per symbol for ``_CACHE_TTL_SECONDS`` (6h). Set
    ``force_refresh=True`` to bypass the cache.
    """
    now = today or dt.datetime.now(dt.timezone.utc)
    symbol = symbol.upper().strip()

    cached = _CACHE.get(symbol)
    if (not force_refresh) and cached:
        fetched_at, payload = cached
        age = (now - fetched_at).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            return payload

    # Cache miss / forced refresh. Run research, score, store.
    try:
        from .config import settings
        if not settings.anthropic_api_key:
            logger.debug("news_sentiment: ANTHROPIC_API_KEY unset; skipping")
            return None
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        # Compact prompt -- only ask for the news, not the full research blob.
        prompt = (
            f"For the ticker {symbol}, return ONLY a JSON array of recent "
            "news items (last 7 days), each with this shape: "
            '{ "headline": "<text>", "impact": "POSITIVE|NEGATIVE|NEUTRAL", '
            '"magnitude": <integer 1-5>, "rationale": "<one sentence>" }. '
            "Up to 5 items, most impactful first. Use web search."
        )
        msg = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": "Return ONLY a JSON array. No markdown, no preamble.",
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
        )
        text_parts = []
        for b in msg.content:
            if getattr(b, "type", None) == "text":
                text_parts.append(getattr(b, "text", "") or "")
        text = "\n".join(text_parts).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            logger.warning(f"news_sentiment {symbol}: no JSON array in response")
            return None
        import json as _json
        items = _json.loads(text[start:end + 1])
    except Exception as e:
        logger.warning(f"news_sentiment {symbol}: fetch failed: {e}")
        return None

    stats = _score_news_items(items)
    payload = {
        **stats,
        "sentiment_ok": evaluate_sentiment(stats["score"]),
        "fetched_at": now.isoformat(),
        "items": items[:5],  # capped for storage / display
    }
    _CACHE[symbol] = (now, payload)
    return payload


def clear_cache() -> None:
    """Test helper / future force-refresh button."""
    _CACHE.clear()
