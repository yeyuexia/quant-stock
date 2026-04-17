# tests/test_news_shock.py
import datetime as dt
from news_shock import (
    match_headlines, NewsHit, dedupe_by_title_hash, log_hit,
)


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
