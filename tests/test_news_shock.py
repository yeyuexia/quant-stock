# tests/test_news_shock.py
import datetime as dt
from news_shock import (
    match_headlines, NewsHit, dedupe_by_title_hash, log_hit,
)
import logging
import news_shock
import os
import pytest
import sys


def _hl(title, source="test", ts=None):
    return {
        "title": title,
        "source": source,
        "ts": ts or dt.datetime(2026, 4, 17, 13, 45, tzinfo=dt.timezone.utc),
    }


def test_match_headlines_finds_keyword_matches():
    headlines = [
        _hl("Trump announces new tariffs on Chinese imports"),
        _hl("Apple earnings beat expectations"),
        _hl("Fed signals rate cut at next meeting"),
    ]
    keywords = ["tariff", "tariffs", "fed", "rate cut"]
    hits = match_headlines(headlines, keywords, plan_symbols=set())
    titles = [h.title for h in hits]
    assert len(hits) == 2
    assert any("tariffs" in t for t in titles)
    assert any("Fed" in t for t in titles)


def test_match_headlines_picks_up_plan_symbols():
    headlines = [
        _hl("NVDA crashes 20% on guidance cut"),
        _hl("Boring non-market news"),
    ]
    hits = match_headlines(headlines, keywords=[], plan_symbols={"NVDA"})
    assert len(hits) == 1
    assert hits[0].matched == "NVDA"


def test_dedupe_removes_duplicate_title_hashes_within_window():
    now = dt.datetime(2026, 4, 17, 14, 0, tzinfo=dt.timezone.utc)
    hits = [
        NewsHit(title="Fed hints at rate cut", source="a",
                ts=now, matched="fed"),
        NewsHit(title="Fed hints at rate cut", source="b",
                ts=now + dt.timedelta(minutes=10), matched="fed"),
        NewsHit(title="Fed hints at rate cut", source="c",
                ts=now + dt.timedelta(minutes=90), matched="fed"),
    ]
    deduped = dedupe_by_title_hash(hits, window_minutes=60)
    assert len(deduped) == 2


def test_log_hit_appends_to_csv(tmp_path, monkeypatch):
    log_path = tmp_path / "news_log.csv"
    monkeypatch.setattr("news_shock.NEWS_SHOCK_LOG", str(log_path))
    h = NewsHit(title="Fed announces rate cut", source="reuters",
                ts=dt.datetime(2026, 4, 17, 14, 5, tzinfo=dt.timezone.utc),
                matched="fed")
    log_hit(h, corroborated=True)
    content = log_path.read_text()
    assert "Fed announces rate cut" in content
    assert "reuters" in content
    assert "fed" in content
    assert "True" in content


# ======================================================================
# Post-review additions (formerly test_news_shock_optimizations.py)
# ======================================================================

"""Regression tests for news_shock.py hardening — log_hit lock + fetch logging."""
import datetime as dt
import logging
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import news_shock


def test_log_hit_creates_lock_sidecar(tmp_path, monkeypatch):
    log_path = tmp_path / "news.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    hit = news_shock.NewsHit(
        title="Fed surprise rate cut", source="yahoo",
        ts=dt.datetime(2026, 5, 24, 14, tzinfo=dt.timezone.utc),
        matched="rate cut",
    )
    news_shock.log_hit(hit, corroborated=True)
    assert log_path.exists()
    assert (tmp_path / "news.csv.lock").exists()
    content = log_path.read_text()
    assert "Fed surprise rate cut" in content
    assert "True" in content


def test_log_hit_writes_header_once(tmp_path, monkeypatch):
    log_path = tmp_path / "news.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    hit1 = news_shock.NewsHit(
        title="One", source="yahoo",
        ts=dt.datetime(2026, 5, 24, 14, tzinfo=dt.timezone.utc),
        matched="one",
    )
    hit2 = news_shock.NewsHit(
        title="Two", source="yahoo",
        ts=dt.datetime(2026, 5, 24, 15, tzinfo=dt.timezone.utc),
        matched="two",
    )
    news_shock.log_hit(hit1, corroborated=False)
    news_shock.log_hit(hit2, corroborated=True)

    lines = log_path.read_text().splitlines()
    # 1 header + 2 rows
    assert len(lines) == 3
    assert lines[0].startswith("ts,source,matched,corroborated,title")


def test_yahoo_fetch_failure_logs_warning(monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("yfinance API broken")
    monkeypatch.setattr("yfinance.Ticker", boom)
    with caplog.at_level(logging.WARNING, logger="news_shock"):
        out = news_shock._fetch_yahoo_headlines(
            dt.datetime(2026, 5, 24, tzinfo=dt.timezone.utc)
        )
    assert out == []
    assert any("yahoo headlines fetch failed" in r.message for r in caplog.records)


def test_reddit_fetch_failure_logs_warning(monkeypatch, caplog):
    import urllib.request
    def boom(*a, **kw):
        raise RuntimeError("reddit 503")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level(logging.WARNING, logger="news_shock"):
        out = news_shock._fetch_reddit_headlines(
            dt.datetime(2026, 5, 24, tzinfo=dt.timezone.utc)
        )
    assert out == []
    assert any("reddit headlines fetch failed" in r.message for r in caplog.records)
