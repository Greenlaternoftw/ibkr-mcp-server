"""Tests for the news-sentiment scoring (Phase F).

The Anthropic fetch path is integration-tested implicitly via the
live deployment. These tests cover the pure scoring + threshold
logic, which determines whether the news gate blocks an entry.
"""

from __future__ import annotations

import pytest

from ibkr_mcp_server import news_sentiment as ns


class TestScoreNewsItems:
    def test_empty_returns_zero(self):
        out = ns._score_news_items([])
        assert out["score"] == 0
        assert out["n_items"] == 0
        assert out["top_negative"] is None

    def test_positive_only(self):
        items = [
            {"headline": "earnings beat", "impact": "POSITIVE", "magnitude": 4},
            {"headline": "upgrade", "impact": "POSITIVE", "magnitude": 2},
        ]
        out = ns._score_news_items(items)
        assert out["score"] == 6
        assert out["n_positive"] == 2
        assert out["n_negative"] == 0

    def test_negative_only(self):
        items = [
            {"headline": "downgrade", "impact": "NEGATIVE", "magnitude": 3},
            {"headline": "guidance cut", "impact": "NEGATIVE", "magnitude": 5},
        ]
        out = ns._score_news_items(items)
        assert out["score"] == -8
        assert out["n_negative"] == 2
        assert out["top_negative"] == "guidance cut"  # higher magnitude

    def test_mixed_nets(self):
        items = [
            {"headline": "good", "impact": "POSITIVE", "magnitude": 4},
            {"headline": "bad", "impact": "NEGATIVE", "magnitude": 5},
            {"headline": "meh", "impact": "NEUTRAL", "magnitude": 2},
        ]
        out = ns._score_news_items(items)
        assert out["score"] == -1
        assert out["n_positive"] == 1
        assert out["n_negative"] == 1
        assert out["n_neutral"] == 1

    def test_invalid_magnitude_dropped(self):
        items = [
            {"headline": "ok", "impact": "POSITIVE", "magnitude": 3},
            {"headline": "trash", "impact": "POSITIVE", "magnitude": 0},   # invalid
            {"headline": "trash2", "impact": "POSITIVE", "magnitude": 99}, # invalid
        ]
        out = ns._score_news_items(items)
        assert out["score"] == 3
        assert out["n_positive"] == 1  # only the valid one

    def test_invalid_impact_dropped(self):
        items = [
            {"headline": "ok", "impact": "POSITIVE", "magnitude": 3},
            {"headline": "trash", "impact": "MIXED", "magnitude": 2},
        ]
        out = ns._score_news_items(items)
        assert out["score"] == 3

    def test_case_insensitive_impact(self):
        items = [
            {"headline": "x", "impact": "positive", "magnitude": 2},
            {"headline": "y", "impact": "Negative", "magnitude": 1},
        ]
        out = ns._score_news_items(items)
        assert out["score"] == 1


class TestEvaluateSentiment:
    def test_above_threshold_passes(self):
        # Default threshold is -5; score=0 passes
        assert ns.evaluate_sentiment(0) is True

    def test_at_threshold_blocks(self):
        # Strict > comparison: -5 itself is blocked
        assert ns.evaluate_sentiment(-5) is False

    def test_below_threshold_blocks(self):
        assert ns.evaluate_sentiment(-7) is False

    def test_positive_score_passes(self):
        assert ns.evaluate_sentiment(8) is True

    def test_custom_threshold(self):
        # Stricter -2 threshold blocks borderline scores
        assert ns.evaluate_sentiment(-3, block_threshold=-2) is False
        assert ns.evaluate_sentiment(-1, block_threshold=-2) is True

    def test_none_returns_none(self):
        assert ns.evaluate_sentiment(None) is None


class TestLexiconClassifier:
    """Phase F (Path B): yfinance + local keyword scoring.

    Lexicon is intentionally conservative -- the goal is to avoid false
    positives that would block real entries, even at the cost of
    occasionally missing a real negative event. The gate threshold (-5)
    is a second filter; only strong net-negative CLUSTERS block.
    """

    def test_neutral_headline(self):
        impact, mag = ns._classify_headline("AAPL trading volume average for Q2")
        assert impact == "NEUTRAL"
        assert mag == 0

    def test_clear_positive(self):
        impact, mag = ns._classify_headline("Apple beats Q3 earnings estimates")
        assert impact == "POSITIVE"
        assert mag >= 3

    def test_clear_negative(self):
        impact, mag = ns._classify_headline("Tesla downgraded to Sell at Morgan Stanley")
        assert impact == "NEGATIVE"
        assert mag >= 3

    def test_very_negative_caps_at_5(self):
        # Multiple strong negative words → magnitude clamped to 5
        impact, mag = ns._classify_headline(
            "Stock plunges as bankruptcy filing reveals fraud and lawsuit"
        )
        assert impact == "NEGATIVE"
        assert mag == 5

    def test_summary_text_considered(self):
        impact, mag = ns._classify_headline(
            title="Mixed Q3 results",
            summary="Company reports earnings beat with raises guidance for next quarter",
        )
        assert impact == "POSITIVE"
        assert mag >= 3

    def test_offsetting_words_net_to_neutral(self):
        # Equal weights → neutral
        impact, mag = ns._classify_headline(
            "Mixed results: earnings beat but layoffs announced"
        )
        # beat=3, layoffs=3 → net 0 → NEUTRAL
        assert impact == "NEUTRAL"

    def test_case_insensitive(self):
        lower = ns._classify_headline("apple beats earnings")
        upper = ns._classify_headline("APPLE BEATS EARNINGS")
        mixed = ns._classify_headline("Apple Beats Earnings")
        assert lower == upper == mixed
        assert lower[0] == "POSITIVE"

    def test_empty_headline(self):
        impact, mag = ns._classify_headline("")
        assert impact == "NEUTRAL"
        assert mag == 0

    def test_specific_lexicon_terms(self):
        # Spot-check a few high-importance terms each scoring as expected
        assert ns._classify_headline("FDA approval granted for drug X")[0] == "POSITIVE"
        assert ns._classify_headline("Company files Chapter 11 bankruptcy")[0] == "NEGATIVE"
        assert ns._classify_headline("Q3 results: company misses revenue")[0] == "NEGATIVE"
        assert ns._classify_headline("Board announces buyback of $5B")[0] == "POSITIVE"

    def test_misleading_keyword_not_overweighted(self):
        # "high" alone shouldn't move sentiment (the word is too vague)
        impact, mag = ns._classify_headline("Stock hits new high for the year")
        # Should be NEUTRAL because "high" isn't in our lexicon
        # (intentional design choice -- avoids false positives)
        assert impact == "NEUTRAL"


class TestCache:
    def teardown_method(self):
        ns.clear_cache()

    def test_clear_cache_empties_state(self):
        # Plant a fake cache entry, clear, verify gone
        import datetime as dt
        ns._CACHE["TEST"] = (dt.datetime.now(dt.timezone.utc), {"score": 5})
        assert "TEST" in ns._CACHE
        ns.clear_cache()
        assert "TEST" not in ns._CACHE
