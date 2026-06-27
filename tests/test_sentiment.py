"""Regression tests for sentiment.py hardening — word-boundary matching,
fetch failure logging, atomic cache writes."""
import json
import logging
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.signals.sentiment as sentiment


# ── SE5: word-boundary matching (was substring) ───────────────────

def test_score_text_does_not_fire_bear_on_bearable():
    """'bearable' must not count as a bearish hit (old substring match did)."""
    score, label = sentiment._score_text("The pain was bearable today")
    # No bull/bear words at all → neutral
    assert label == "neutral"
    assert score == 0.0


def test_score_text_does_not_fire_bull_on_bullshit():
    score, label = sentiment._score_text("That report was bullshit honestly")
    assert label == "neutral"
    assert score == 0.0


def test_score_text_does_fire_on_real_bull_word():
    score, label = sentiment._score_text("Major rally today, SPY breakout!")
    assert label == "bullish"
    assert score > 0


def test_score_text_does_fire_on_real_bear_word():
    score, label = sentiment._score_text("Market crash imminent, sell everything")
    assert label == "bearish"
    assert score < 0


def test_score_text_multi_word_phrases_still_match():
    """Multi-word phrases like 'earnings beat' / 'rug pull' fall back to
    substring `in` (already specific enough). Verify they still fire."""
    score, label = sentiment._score_text("NVDA earnings beat estimates")
    assert label == "bullish"


# ── SE1: atomic cache writes ──────────────────────────────────────

def test_cache_set_creates_lock_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(sentiment, "CACHE_DIR", str(tmp_path))
    sentiment._cache_set("foo", {"x": 1})
    target = tmp_path / "sentiment_foo.json"
    assert target.exists()
    assert (tmp_path / "sentiment_foo.json.lock").exists()
    assert json.loads(target.read_text()) == {"x": 1}


# ── SE2: fetch failures log warnings ──────────────────────────────

def test_fetch_reddit_posts_logs_failure(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(sentiment, "CACHE_DIR", str(tmp_path))
    import urllib.request
    def boom(*a, **kw):
        raise RuntimeError("reddit 503")
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    with caplog.at_level(logging.WARNING, logger="sentiment"):
        out = sentiment.fetch_reddit_posts("stocks", limit=5)
    assert out == []
    assert any("fetch_reddit_posts" in r.message for r in caplog.records)


def test_fetch_yf_news_logs_failure(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(sentiment, "CACHE_DIR", str(tmp_path))

    def boom(t):
        raise RuntimeError("yfinance dead")
    monkeypatch.setattr("yfinance.Ticker", boom)

    with caplog.at_level(logging.WARNING, logger="sentiment"):
        out = sentiment.fetch_yf_news(tickers=["SPY"])
    # Empty result (no successful fetches)
    assert out == []
    assert any("fetch_yf_news" in r.message for r in caplog.records)
