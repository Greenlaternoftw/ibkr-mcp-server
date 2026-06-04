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
