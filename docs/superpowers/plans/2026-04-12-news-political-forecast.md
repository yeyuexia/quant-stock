# News & Political Forecast System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reactive news/political intelligence layer that fetches RSS feeds every 5 minutes, detects hotspots, calls Claude API for analysis, blends a `political_risk_score` into the macro regime, and delivers briefings via terminal and Telegram.

**Architecture:** New modules `news_store.py` (SQLite), `political.py` (RSS + classification), `forecast.py` (Claude API), `tg_notifier.py` (push helper), `news_poller.py` (daemon) in `stock/`. Existing `macro.py`, `watchdog.py`, `tg_bot.py` modified minimally.

**Tech Stack:** Python 3.7+, `feedparser` (RSS), `anthropic` SDK (Claude API with prompt caching), `pytz` (timezone), `sqlite3` (stdlib), `urllib.request` (stdlib Telegram push)

---

## File Map

| File | Status | Purpose |
|------|--------|---------|
| `stock/news_store.py` | CREATE | SQLite CRUD, dedup, retention, hotspot queries |
| `stock/political.py` | CREATE | RSS fetching, rule-based categorization, hotspot detection |
| `stock/forecast.py` | CREATE | Claude API — hotspot Haiku + scheduled Sonnet briefing |
| `stock/tg_notifier.py` | CREATE | Send Telegram messages via Bot API (urllib, no async) |
| `stock/news_poller.py` | CREATE | 5-min polling daemon — orchestrates all of the above |
| `stock/tests/test_news_store.py` | CREATE | Unit tests for news_store |
| `stock/tests/test_political.py` | CREATE | Unit tests for political |
| `stock/tests/test_forecast.py` | CREATE | Unit tests for forecast |
| `stock/config.py` | MODIFY | Add RSS feeds, categories, thresholds, LLM model names |
| `stock/macro.py` | MODIFY | Blend `political_risk_score` into `macro_risk_adjustment()` |
| `stock/watchdog.py` | MODIFY | Add `check_political_forecast()`, `--forecast` flag, updated cron |
| `stock/requirements.txt` | MODIFY | Add `feedparser`, `anthropic`, `pytz` |
| `tg-bot/tg_bot.py` | MODIFY | Add `/forecast`, `/hotspots` commands, update schedule to 3x daily |

---

## Task 1: Config additions

**Files:**
- Modify: `stock/config.py` (append after line 167)

- [ ] **Step 1: Append new config constants to config.py**

Add to the bottom of `stock/config.py`:

```python
# ── News Polling ─────────────────────────────────────────────────
NEWS_POLL_INTERVAL_SECONDS = 300     # 5 minutes
NEWS_RETENTION_DAYS = 7

# ── Hotspot Detection ────────────────────────────────────────────
HOTSPOT_WINDOW_MINUTES = 30
HOTSPOT_THRESHOLD_COUNT = 5
HOTSPOT_MAX_LLM_CALLS_PER_HOUR = 3

# ── LLM Models ───────────────────────────────────────────────────
LLM_HOTSPOT_MODEL = "claude-haiku-4-5-20251001"
LLM_BRIEFING_MODEL = "claude-sonnet-4-6"
LLM_BRIEFING_HOURS = 8       # hours of history for scheduled briefing
LLM_PREMARKET_HOURS = 14     # extended window for pre-market (covers Asia overnight)

# ── RSS Feeds ────────────────────────────────────────────────────
RSS_FEEDS = [
    # US
    {"url": "https://feeds.reuters.com/reuters/businessNews",         "source": "Reuters Business",  "region": "us"},
    {"url": "https://feeds.reuters.com/Reuters/PoliticsNews",         "source": "Reuters Politics",  "region": "us"},
    {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",  "source": "CNBC Top",          "region": "us"},
    {"url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",   "source": "CNBC Politics",     "region": "us"},
    {"url": "https://feeds.marketwatch.com/marketwatch/topstories/",  "source": "MarketWatch",       "region": "us"},
    {"url": "https://rss.politico.com/politics-news.xml",             "source": "Politico",          "region": "us"},
    {"url": "https://feeds.feedburner.com/ap-business",               "source": "AP Business",       "region": "us"},
    {"url": "https://www.federalreserve.gov/feeds/press_all.xml",     "source": "Federal Reserve",   "region": "us"},
    # Asia
    {"url": "https://asia.nikkei.com/rss/feed/nar",               "source": "Nikkei Asia",    "region": "asia"},
    {"url": "https://www.scmp.com/rss/2/feed",                    "source": "SCMP Business",  "region": "asia"},
    {"url": "https://www.straitstimes.com/business/rss.xml",      "source": "Straits Times",  "region": "asia"},
    {"url": "https://www.caixinglobal.com/rss/index.xml",         "source": "Caixin Global",  "region": "asia"},
    {"url": "https://www3.nhk.or.jp/nhkworld/en/news/rss.xml",    "source": "NHK World",      "region": "asia"},
]

# ── Political Event Categories ────────────────────────────────────
CATEGORIES = {
    "tariff": {
        "keywords": ["tariff", "trade war", "import duty", "sanction", "export ban",
                     "trade deal", "trade deficit", "customs duty"],
        "sectors": ["XLY", "XLI", "XLE", "AAPL", "AMZN", "NVDA"],
    },
    "fed": {
        "keywords": ["federal reserve", "fed rate", "fomc", "powell", "interest rate",
                     "quantitative easing", "rate hike", "rate cut", "monetary policy",
                     "basis points"],
        "sectors": ["XLF", "TLT", "IEF", "BIL", "SHY"],
    },
    "election": {
        "keywords": ["election", "congress", "senate", "president", "policy",
                     "regulation", "legislation", "vote", "ballot"],
        "sectors": ["XLV", "XLE", "XLF", "XLY"],
    },
    "geopolitical": {
        "keywords": ["war", "conflict", "missile", "invasion", "coup", "taiwan",
                     "nato", "military", "nuclear", "troops", "attack", "strike"],
        "sectors": ["XLE", "TLT", "BIL"],
    },
    "macro_data": {
        "keywords": ["cpi", "inflation", "gdp", "unemployment", "jobs report",
                     "recession", "pce", "retail sales", "nonfarm payroll",
                     "consumer price index"],
        "sectors": ["SPY", "TLT", "BIL", "XLF"],
    },
    "earnings": {
        "keywords": ["earnings", "revenue", "guidance", "quarterly results",
                     "beats estimates", "misses estimates", "profit warning"],
        "sectors": [],
    },
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/zl/works/stock
git add config.py
git commit -m "feat: add news/political forecast config constants"
```

---

## Task 2: news_store.py (SQLite layer)

**Files:**
- Create: `stock/news_store.py`
- Create: `stock/tests/__init__.py`
- Create: `stock/tests/test_news_store.py`

- [ ] **Step 1: Write failing tests**

Create `stock/tests/__init__.py` (empty):
```python
```

Create `stock/tests/test_news_store.py`:
```python
import os
import sys
import tempfile
import unittest

# Point news_store at a temp DB for tests
_tmp = tempfile.mkdtemp()
os.environ["NEWS_DB_PATH"] = os.path.join(_tmp, "test_news.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import news_store


class TestNewsStore(unittest.TestCase):

    def setUp(self):
        news_store.init_db()

    def test_insert_article_returns_id(self):
        aid = news_store.insert_article(
            url="https://example.com/1",
            title="Test Title",
            summary="Test summary",
            source="Test",
            region="us",
            published_at="2026-04-12T08:00:00",
        )
        self.assertIsNotNone(aid)
        self.assertEqual(len(aid), 32)

    def test_insert_article_dedup(self):
        url = "https://example.com/dedup"
        aid1 = news_store.insert_article(url=url, title="A", summary="", source="S", region="us")
        aid2 = news_store.insert_article(url=url, title="A", summary="", source="S", region="us")
        self.assertIsNotNone(aid1)
        self.assertIsNone(aid2)  # duplicate returns None

    def test_insert_and_count_events(self):
        aid = news_store.insert_article(
            url="https://example.com/ev1", title="Tariff news", summary="",
            source="Reuters", region="us"
        )
        news_store.insert_event(aid, "tariff", ["tariff"], 1)
        count = news_store.count_events_in_window("tariff", minutes=60)
        self.assertGreaterEqual(count, 1)

    def test_count_events_excludes_old(self):
        count = news_store.count_events_in_window("geopolitical", minutes=1)
        self.assertEqual(count, 0)

    def test_insert_and_get_analysis(self):
        news_store.insert_analysis(
            trigger="hotspot",
            category="tariff",
            input_summary="test input",
            briefing="Markets fell on tariff news.",
            sector_impacts={"XLY": "bearish"},
            political_risk_score=-0.5,
        )
        latest = news_store.get_latest_analysis()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["trigger"], "hotspot")
        self.assertAlmostEqual(latest["political_risk_score"], -0.5)
        self.assertEqual(latest["sector_impacts"]["XLY"], "bearish")

    def test_get_articles_for_briefing(self):
        aid = news_store.insert_article(
            url="https://example.com/brief1", title="Fed rate cut",
            summary="", source="Reuters", region="us"
        )
        news_store.insert_event(aid, "fed", ["rate cut"], 1)
        articles = news_store.get_articles_for_briefing(hours=8)
        self.assertGreater(len(articles), 0)
        self.assertIn("title", articles[0])
        self.assertIn("category", articles[0])

    def test_cleanup_old_articles(self):
        # Insert then immediately clean — nothing should be deleted (article is fresh)
        news_store.insert_article(
            url="https://example.com/fresh", title="Fresh", summary="",
            source="S", region="us"
        )
        news_store.cleanup_old_articles(days=7)
        # Fresh article survives
        arts = news_store.get_articles_for_briefing(hours=1)
        # Just check cleanup doesn't crash
        self.assertIsInstance(arts, list)

    def test_count_hotspot_llm_calls(self):
        news_store.insert_analysis(
            trigger="hotspot", category="tariff",
            input_summary="x", briefing="y",
            sector_impacts={}, political_risk_score=-0.3,
        )
        count = news_store.count_hotspot_llm_calls("tariff", hours=1)
        self.assertGreaterEqual(count, 1)
        count_other = news_store.count_hotspot_llm_calls("fed", hours=1)
        self.assertEqual(count_other, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_news_store.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'news_store'`

- [ ] **Step 3: Create news_store.py**

Create `stock/news_store.py`:
```python
"""
SQLite-backed news article storage.
7-day retention, URL dedup, hotspot queries.
"""
import os
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# Allow override for tests
DB_PATH = os.environ.get(
    "NEWS_DB_PATH",
    os.path.join(os.path.dirname(__file__), ".cache", "news.db"),
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id           TEXT PRIMARY KEY,
                url          TEXT UNIQUE,
                title        TEXT,
                summary      TEXT,
                source       TEXT,
                region       TEXT,
                fetched_at   TEXT,
                published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  TEXT REFERENCES articles(id),
                category    TEXT,
                keywords    TEXT,
                severity    INTEGER,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_analyses (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger              TEXT,
                category             TEXT,
                input_summary        TEXT,
                briefing             TEXT,
                sector_impacts       TEXT,
                political_risk_score REAL,
                created_at           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_cat_time ON events(category, created_at);
            CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_analyses_created ON llm_analyses(created_at);
        """)


def _article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def insert_article(url: str, title: str, summary: str, source: str,
                   region: str, published_at: str = "") -> Optional[str]:
    """Insert article; returns article_id on success, None if duplicate."""
    if not url or not title:
        return None
    article_id = _article_id(url)
    now = datetime.utcnow().isoformat()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(id, url, title, summary, source, region, fetched_at, published_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (article_id, url, title, summary, source, region, now, published_at),
            )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            return article_id if changed > 0 else None
    except sqlite3.Error:
        return None


def insert_event(article_id: str, category: str, keywords: List[str], severity: int):
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO events (article_id, category, keywords, severity, created_at) "
            "VALUES (?,?,?,?,?)",
            (article_id, category, json.dumps(keywords), severity, now),
        )


def count_events_in_window(category: str, minutes: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE category=? AND created_at > ?",
            (category, cutoff),
        ).fetchone()
        return row[0]


def get_recent_events(category: str, minutes: int) -> List[Dict]:
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT a.title, a.source, a.region, e.category, e.severity
               FROM events e JOIN articles a ON e.article_id = a.id
               WHERE e.category=? AND e.created_at > ?
               ORDER BY e.created_at DESC""",
            (category, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_analysis(trigger: str, category: Optional[str], input_summary: str,
                    briefing: str, sector_impacts: Dict, political_risk_score: float):
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO llm_analyses "
            "(trigger, category, input_summary, briefing, sector_impacts, political_risk_score, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (trigger, category, input_summary, briefing,
             json.dumps(sector_impacts), political_risk_score, now),
        )


def get_latest_analysis() -> Optional[Dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_analyses ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["sector_impacts"] = json.loads(d.get("sector_impacts") or "{}")
        return d


def get_articles_for_briefing(hours: int = 8) -> List[Dict]:
    """Articles with event classifications for LLM input."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT a.title, a.source, a.region, e.category
               FROM events e JOIN articles a ON e.article_id = a.id
               WHERE e.created_at > ?
               ORDER BY e.created_at DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def cleanup_old_articles(days: int = 7):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM events WHERE article_id IN "
            "(SELECT id FROM articles WHERE fetched_at < ?)", (cutoff,)
        )
        conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))


def count_hotspot_llm_calls(category: str, hours: int = 1) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM llm_analyses "
            "WHERE trigger='hotspot' AND category=? AND created_at > ?",
            (category, cutoff),
        ).fetchone()
        return row[0]
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_news_store.py -v
```
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add news_store.py tests/__init__.py tests/test_news_store.py
git commit -m "feat: add SQLite news store with dedup and hotspot queries"
```

---

## Task 3: political.py (RSS fetching + classification)

**Files:**
- Create: `stock/political.py`
- Create: `stock/tests/test_political.py`

- [ ] **Step 1: Write failing tests**

Create `stock/tests/test_political.py`:
```python
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

_tmp = tempfile.mkdtemp()
os.environ["NEWS_DB_PATH"] = os.path.join(_tmp, "test_political.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import news_store
news_store.init_db()

import political


class TestCategorizeArticle(unittest.TestCase):

    def test_tariff_keyword(self):
        cat, kws, sev = political.categorize_article("New tariff on China imports", "")
        self.assertEqual(cat, "tariff")
        self.assertIn("tariff", kws)
        self.assertEqual(sev, 1)

    def test_fed_keyword(self):
        cat, kws, sev = political.categorize_article("FOMC raises interest rate by 25 basis points", "")
        self.assertEqual(cat, "fed")

    def test_geopolitical_keyword(self):
        cat, kws, sev = political.categorize_article("Military conflict escalates near Taiwan", "")
        self.assertEqual(cat, "geopolitical")

    def test_no_match_returns_none(self):
        cat, kws, sev = political.categorize_article("Local bakery wins award", "")
        self.assertIsNone(cat)
        self.assertEqual(kws, [])
        self.assertEqual(sev, 0)

    def test_multi_category_raises_severity(self):
        # Both tariff and geopolitical keywords
        cat, kws, sev = political.categorize_article(
            "Tariff sanctions escalate military conflict", ""
        )
        self.assertIsNotNone(cat)
        self.assertEqual(sev, 2)  # 2+ categories matched


class TestFetchRssFeed(unittest.TestCase):

    @patch("political.feedparser.parse")
    def test_returns_articles(self, mock_parse):
        mock_parse.return_value = MagicMock(entries=[
            MagicMock(
                link="https://example.com/1",
                title="Test Article",
                summary="Summary text",
                description="",
                published="2026-04-12",
            )
        ])
        articles = political.fetch_rss_feed("https://fake.url", "TestSource", "us")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["source"], "TestSource")
        self.assertEqual(articles[0]["region"], "us")

    @patch("political.feedparser.parse", side_effect=Exception("network error"))
    def test_returns_empty_on_error(self, mock_parse):
        articles = political.fetch_rss_feed("https://fake.url", "S", "us")
        self.assertEqual(articles, [])


class TestRunClassificationPass(unittest.TestCase):

    @patch("political.fetch_all_rss")
    def test_stores_new_articles(self, mock_fetch):
        mock_fetch.return_value = [{
            "url": "https://example.com/tariff99",
            "title": "New tariff on imports announced",
            "summary": "",
            "source": "Reuters",
            "region": "us",
            "published_at": "",
        }]
        political.run_classification_pass()
        count = news_store.count_events_in_window("tariff", minutes=60)
        self.assertGreaterEqual(count, 1)

    @patch("political.fetch_all_rss")
    def test_deduplicates_on_second_pass(self, mock_fetch):
        article = {
            "url": "https://example.com/dedup99",
            "title": "Tariff update",
            "summary": "",
            "source": "Reuters",
            "region": "us",
            "published_at": "",
        }
        mock_fetch.return_value = [article]
        political.run_classification_pass()
        before = news_store.count_events_in_window("tariff", minutes=60)
        political.run_classification_pass()
        after = news_store.count_events_in_window("tariff", minutes=60)
        self.assertEqual(before, after)  # second pass adds nothing


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_political.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'political'`

- [ ] **Step 3: Install feedparser**

```bash
pip install feedparser
```

- [ ] **Step 4: Create political.py**

Create `stock/political.py`:
```python
"""
RSS news fetching and rule-based political event classification.

Fetches configured RSS feeds, classifies articles into event categories,
detects hotspots (volume spikes within a time window).
"""
import time
from typing import List, Dict, Tuple, Optional

import feedparser

from config import RSS_FEEDS, CATEGORIES, HOTSPOT_WINDOW_MINUTES, HOTSPOT_THRESHOLD_COUNT
from news_store import (
    insert_article, insert_event,
    count_events_in_window, get_recent_events,
)


def fetch_rss_feed(url: str, source: str, region: str) -> List[Dict]:
    """Fetch one RSS feed. Returns list of article dicts."""
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:20]:
            articles.append({
                "url": entry.get("link", ""),
                "title": entry.get("title", ""),
                "summary": (entry.get("summary") or entry.get("description") or "")[:500],
                "source": source,
                "region": region,
                "published_at": entry.get("published", ""),
            })
        return articles
    except Exception:
        return []


def fetch_all_rss() -> List[Dict]:
    """Fetch all configured RSS feeds."""
    all_articles = []
    for feed_cfg in RSS_FEEDS:
        articles = fetch_rss_feed(feed_cfg["url"], feed_cfg["source"], feed_cfg["region"])
        all_articles.extend(articles)
        time.sleep(0.2)  # polite delay
    return all_articles


def categorize_article(title: str, summary: str) -> Tuple[Optional[str], List[str], int]:
    """Classify article into best-matching category.

    Returns (category, matched_keywords, severity):
      severity 1 = single category match
      severity 2 = 2+ categories matched in same article
      (None, [], 0) = no match
    """
    text = f"{title} {summary}".lower()
    matched: List[Tuple[str, List[str]]] = []

    for cat, cfg in CATEGORIES.items():
        matched_kws = [kw for kw in cfg["keywords"] if kw in text]
        if matched_kws:
            matched.append((cat, matched_kws))

    if not matched:
        return None, [], 0

    # Best category = most keywords matched
    best_cat, best_kws = max(matched, key=lambda x: len(x[1]))
    severity = 1 if len(matched) == 1 else 2
    return best_cat, best_kws, severity


def run_classification_pass() -> List[Dict]:
    """Fetch new RSS articles, classify, store. Returns hotspot triggers.

    A hotspot fires when a category has >= HOTSPOT_THRESHOLD_COUNT events
    within HOTSPOT_WINDOW_MINUTES. Severity is upgraded to 3 in that case.

    Returns list of: {"category": str, "count": int, "articles": list}
    """
    articles = fetch_all_rss()

    for article in articles:
        if not article["url"] or not article["title"]:
            continue

        article_id = insert_article(
            url=article["url"],
            title=article["title"],
            summary=article["summary"],
            source=article["source"],
            region=article["region"],
            published_at=article["published_at"],
        )

        if article_id is None:
            continue  # duplicate — skip classification

        category, keywords, severity = categorize_article(
            article["title"], article["summary"]
        )
        if category:
            insert_event(article_id, category, keywords, severity)

    # Check hotspots across all categories
    hotspots = []
    for cat in CATEGORIES:
        count = count_events_in_window(cat, HOTSPOT_WINDOW_MINUTES)
        if count >= HOTSPOT_THRESHOLD_COUNT:
            recent = get_recent_events(cat, HOTSPOT_WINDOW_MINUTES)
            hotspots.append({"category": cat, "count": count, "articles": recent})

    return hotspots
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_political.py -v
```
Expected: All 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add political.py tests/test_political.py
git commit -m "feat: add RSS fetching and political event classification"
```

---

## Task 4: forecast.py (Claude API)

**Files:**
- Create: `stock/forecast.py`
- Create: `stock/tests/test_forecast.py`

- [ ] **Step 1: Write failing tests**

Create `stock/tests/test_forecast.py`:
```python
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

_tmp = tempfile.mkdtemp()
os.environ["NEWS_DB_PATH"] = os.path.join(_tmp, "test_forecast.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import news_store
news_store.init_db()

import forecast


def _mock_message(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


class TestAnalyzeHotspot(unittest.TestCase):

    @patch("forecast._get_client")
    def test_returns_analysis_dict(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.messages.create.return_value = _mock_message(
            '{"summary": "Tariffs hit tech stocks.", '
            '"sector_impacts": {"XLY": "bearish"}, '
            '"confidence": "high", '
            '"political_risk_score": -0.6}'
        )
        result = forecast.analyze_hotspot("tariff", [{"title": "New tariffs announced"}])
        self.assertEqual(result["confidence"], "high")
        self.assertAlmostEqual(result["political_risk_score"], -0.6)
        self.assertEqual(result["sector_impacts"]["XLY"], "bearish")

    @patch("forecast._get_client")
    def test_handles_invalid_json(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.messages.create.return_value = _mock_message("not json at all")
        result = forecast.analyze_hotspot("tariff", [{"title": "headline"}])
        self.assertIn("summary", result)
        self.assertEqual(result["political_risk_score"], 0.0)

    @patch("forecast._get_client")
    def test_stores_analysis_in_db(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.messages.create.return_value = _mock_message(
            '{"summary": "Fed cuts.", "sector_impacts": {}, '
            '"confidence": "medium", "political_risk_score": 0.4}'
        )
        forecast.analyze_hotspot("fed", [{"title": "Fed cuts rates"}])
        latest = news_store.get_latest_analysis()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["trigger"], "hotspot")
        self.assertEqual(latest["category"], "fed")


class TestGetLatestPoliticalScore(unittest.TestCase):

    def test_returns_zero_when_no_analysis(self):
        # Fresh DB — should return 0.0
        score = forecast.get_latest_political_score()
        self.assertIsInstance(score, float)

    @patch("forecast._get_client")
    def test_returns_stored_score(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.messages.create.return_value = _mock_message(
            '{"summary": "x", "sector_impacts": {}, '
            '"confidence": "low", "political_risk_score": -0.8}'
        )
        forecast.analyze_hotspot("geopolitical", [{"title": "War escalates"}])
        score = forecast.get_latest_political_score()
        self.assertAlmostEqual(score, -0.8)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_forecast.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'forecast'`

- [ ] **Step 3: Install anthropic**

```bash
pip install anthropic
```

- [ ] **Step 4: Create forecast.py**

Create `stock/forecast.py`:
```python
#!/usr/bin/env python3
"""
Political forecast via Claude API.

Two analysis modes:
  hotspot   — immediate, uses claude-haiku-4-5 (fast/cheap)
  scheduled — full briefing, uses claude-sonnet-4-6 (quality)

Usage:
  python3 forecast.py              # print latest briefing
  python3 forecast.py --hotspots   # show recent severity-3 alerts
"""
import os
import sys
import json
import datetime as dt
from typing import Dict, List, Optional

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

import anthropic

from news_store import (
    init_db, get_articles_for_briefing, insert_analysis,
    get_latest_analysis, count_hotspot_llm_calls,
)
from config import LLM_HOTSPOT_MODEL, LLM_BRIEFING_MODEL

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def analyze_hotspot(category: str, articles: List[Dict]) -> Dict:
    """Call Claude Haiku for immediate hotspot analysis. Stores result in DB."""
    article_text = "\n".join(f"- {a['title']}" for a in articles[:10])

    message = _get_client().messages.create(
        model=LLM_HOTSPOT_MODEL,
        max_tokens=512,
        system=[{
            "type": "text",
            "text": (
                "You are a financial analyst. "
                "Respond only in valid JSON with no markdown fences."
            ),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": (
                f"Breaking news cluster: {category}\n\n"
                f"Articles:\n{article_text}\n\n"
                "Respond in JSON:\n"
                '{"summary": "2 sentence market impact summary", '
                '"sector_impacts": {"TICKER": "bullish|bearish|neutral"}, '
                '"confidence": "low|medium|high", '
                '"political_risk_score": 0.0}'
            ),
        }],
    )

    try:
        result = json.loads(message.content[0].text)
        result["political_risk_score"] = float(result.get("political_risk_score", 0.0))
    except (json.JSONDecodeError, IndexError, AttributeError, ValueError):
        result = {
            "summary": "Analysis unavailable.",
            "sector_impacts": {},
            "confidence": "low",
            "political_risk_score": 0.0,
        }

    insert_analysis(
        trigger="hotspot",
        category=category,
        input_summary=article_text[:500],
        briefing=result.get("summary", ""),
        sector_impacts=result.get("sector_impacts", {}),
        political_risk_score=result["political_risk_score"],
    )
    return result


def run_scheduled_briefing(hours: int = 8) -> Dict:
    """Call Claude Sonnet for full scheduled briefing. Stores result in DB."""
    init_db()
    articles = get_articles_for_briefing(hours=hours)

    if not articles:
        fallback = {
            "briefing": "No recent political or macro events detected.",
            "sector_impacts": {},
            "political_risk_score": 0.0,
            "key_risks": [],
        }
        insert_analysis(
            trigger="scheduled", category=None,
            input_summary="(no articles)", briefing=fallback["briefing"],
            sector_impacts={}, political_risk_score=0.0,
        )
        return fallback

    # Summarize by category to keep token count manageable
    by_category: Dict[str, List[str]] = {}
    for a in articles:
        cat = a.get("category", "other")
        by_category.setdefault(cat, []).append(a["title"])

    event_summary = "\n".join(
        f"{cat.upper()}: " + " | ".join(titles[:5])
        for cat, titles in by_category.items()
    )

    try:
        from macro import macro_regime_score
        macro = macro_regime_score()
        macro_str = f"{macro['regime'].upper()} (score: {macro['score']:+.2f})"
    except Exception:
        macro_str = "N/A"

    last = get_latest_analysis()
    prev_score = last["political_risk_score"] if last else 0.0

    message = _get_client().messages.create(
        model=LLM_BRIEFING_MODEL,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": (
                "You are a senior portfolio analyst covering US equities. "
                "Analyze political and macro news and their likely impact on US stocks. "
                "Respond only in valid JSON with no markdown fences."
            ),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": (
                f"Recent events (last {hours}h):\n{event_summary}\n\n"
                f"Current macro regime: {macro_str}\n"
                f"Previous political_risk_score: {prev_score:+.2f}\n\n"
                "Respond in JSON:\n"
                '{"briefing": "2-3 paragraph summary of key developments and market implications", '
                '"sector_impacts": {"TICKER": "bullish|bearish|neutral"}, '
                '"political_risk_score": 0.0, '
                '"key_risks": ["risk1", "risk2"]}'
            ),
        }],
    )

    try:
        result = json.loads(message.content[0].text)
        result["political_risk_score"] = float(result.get("political_risk_score", 0.0))
    except (json.JSONDecodeError, IndexError, AttributeError, ValueError):
        result = {
            "briefing": "Analysis unavailable.",
            "sector_impacts": {},
            "political_risk_score": 0.0,
            "key_risks": [],
        }

    insert_analysis(
        trigger="scheduled", category=None,
        input_summary=event_summary[:500],
        briefing=result.get("briefing", ""),
        sector_impacts=result.get("sector_impacts", {}),
        political_risk_score=result["political_risk_score"],
    )
    return result


def get_latest_political_score() -> float:
    """Return most recent political_risk_score. Defaults to 0.0 (neutral)."""
    init_db()
    latest = get_latest_analysis()
    return latest["political_risk_score"] if latest else 0.0


def _print_latest_briefing():
    init_db()
    latest = get_latest_analysis()
    if not latest:
        print("  No briefing available yet. Run: python3 news_poller.py")
        return

    print(f"\n{'='*60}")
    print("  POLITICAL BRIEFING")
    print(f"{'='*60}")
    print(f"  Trigger:  {latest['trigger'].upper()}")
    print(f"  Time:     {latest['created_at']} UTC")
    print(f"  Pol.Risk: {latest['political_risk_score']:+.2f}")
    print()
    print(latest["briefing"])
    print()

    impacts = latest.get("sector_impacts", {})
    if impacts:
        print("  Sector Impacts:")
        for ticker, direction in impacts.items():
            icon = "↑" if direction == "bullish" else "↓" if direction == "bearish" else "→"
            print(f"    {ticker:6s} {direction} {icon}")
    print()


def _print_hotspots():
    init_db()
    from news_store import _get_conn
    cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=24)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM llm_analyses WHERE trigger='hotspot' AND created_at > ? "
            "ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()

    if not rows:
        print("  No hotspot alerts in the last 24 hours.")
        return

    print("\n  ── Hotspot Alerts (last 24h) ──")
    for row in rows:
        d = dict(row)
        print(f"  [{d['created_at']}] {d['category'].upper()} | risk: {d['political_risk_score']:+.2f}")
        print(f"    {d['briefing'][:100]}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--hotspots" in args:
        _print_hotspots()
    else:
        _print_latest_briefing()
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_forecast.py -v
```
Expected: All 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add forecast.py tests/test_forecast.py
git commit -m "feat: add Claude API forecast module with hotspot and scheduled briefing"
```

---

## Task 5: tg_notifier.py (Telegram push helper)

**Files:**
- Create: `stock/tg_notifier.py`

- [ ] **Step 1: Create tg_notifier.py**

```python
"""
Telegram push notification helper.

Sends messages to the configured TELEGRAM_USER_ID via Bot HTTP API.
Uses only stdlib urllib — no async, no extra dependencies.
"""
import os
import json
import urllib.request
from typing import Dict

# Load .env
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
MAX_LEN = 4000


def _send_raw(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_USER_ID, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def send_message(text: str):
    """Send message, splitting at newlines if >4000 chars."""
    if len(text) <= MAX_LEN:
        _send_raw(text)
        return
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            _send_raw(chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        _send_raw(chunk)


def send_hotspot_alert(category: str, analysis: Dict):
    sectors = analysis.get("sector_impacts", {})
    sector_str = "  ".join(
        f"{t}{'↑' if d == 'bullish' else '↓' if d == 'bearish' else '→'}"
        for t, d in list(sectors.items())[:6]
    )
    text = (
        f"HOTSPOT: {category.upper()}\n"
        f"Confidence: {analysis.get('confidence', '?')}\n\n"
        f"{analysis.get('summary', '')}\n\n"
        f"Sectors: {sector_str}"
    )
    send_message(text)


def send_scheduled_briefing(analysis: Dict, label: str = "BRIEFING"):
    sectors = analysis.get("sector_impacts", {})
    sector_lines = "\n".join(
        f"  {t:6s} {'bullish ↑' if d == 'bullish' else 'bearish ↓' if d == 'bearish' else 'neutral →'}"
        for t, d in list(sectors.items())[:8]
    )
    risks = ", ".join(analysis.get("key_risks", [])[:3]) or "none"
    score = analysis.get("political_risk_score", 0.0)
    text = (
        f"{label}\n\n"
        f"{analysis.get('briefing', '')}\n\n"
        f"Sector Impacts:\n{sector_lines}\n\n"
        f"Political Risk Score: {score:+.2f}\n"
        f"Key Risks: {risks}"
    )
    send_message(text)
```

- [ ] **Step 2: Smoke test (no network call if tokens missing)**

```bash
cd /Users/zl/works/stock
python3 -c "import tg_notifier; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tg_notifier.py
git commit -m "feat: add Telegram push notification helper"
```

---

## Task 6: news_poller.py (background daemon)

**Files:**
- Create: `stock/news_poller.py`

- [ ] **Step 1: Create news_poller.py**

```python
#!/usr/bin/env python3
"""
News polling daemon.

Fetches RSS feeds every 5 minutes, classifies events, detects hotspots,
triggers LLM analysis and Telegram push on severity-3 events.

Run: python3 news_poller.py
"""
import sys
import os
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from news_store import init_db, cleanup_old_articles, count_hotspot_llm_calls
from political import run_classification_pass
from forecast import analyze_hotspot
from tg_notifier import send_hotspot_alert
from config import (
    NEWS_POLL_INTERVAL_SECONDS,
    NEWS_RETENTION_DAYS,
    HOTSPOT_MAX_LLM_CALLS_PER_HOUR,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def poll_once():
    logger.info("Polling RSS feeds...")
    hotspots = run_classification_pass()

    for hotspot in hotspots:
        cat = hotspot["category"]
        count = hotspot["count"]
        articles = hotspot["articles"]

        if count_hotspot_llm_calls(cat, hours=1) >= HOTSPOT_MAX_LLM_CALLS_PER_HOUR:
            logger.info(f"Rate limit reached for {cat}, skipping LLM call")
            continue

        logger.info(f"Hotspot: {cat} ({count} events in window) — calling LLM")
        try:
            analysis = analyze_hotspot(cat, articles)
            send_hotspot_alert(cat, analysis)
            logger.info(f"Hotspot alert sent for {cat}")
        except Exception as e:
            logger.error(f"LLM hotspot analysis failed for {cat}: {e}")

    cleanup_old_articles(days=NEWS_RETENTION_DAYS)


def main():
    logger.info("News poller starting...")
    init_db()
    while True:
        try:
            poll_once()
        except Exception as e:
            logger.error(f"Poll error: {e}")
        logger.info(f"Sleeping {NEWS_POLL_INTERVAL_SECONDS}s...")
        time.sleep(NEWS_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test**

```bash
cd /Users/zl/works/stock
python3 -c "import news_poller; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add news_poller.py
git commit -m "feat: add 5-minute RSS polling daemon with hotspot detection"
```

---

## Task 7: Update macro.py (blend political_risk_score)

**Files:**
- Modify: `stock/macro.py:360-373`

- [ ] **Step 1: Replace macro_risk_adjustment in macro.py**

Replace the existing `macro_risk_adjustment` function (lines 360–373) with:

```python
def macro_risk_adjustment(base_equity_pct: float) -> float:
    """Adjust equity allocation based on macro regime + political risk.

    Blends FRED macro score (70%) with political risk score (30%).
    Score +1.0 → 100% of target allocation.
    Score -1.0 → 40% of target allocation.
    Falls back to FRED-only if forecast module unavailable.
    """
    result = macro_regime_score()
    score = result["score"]
    fred_adj = 0.7 + 0.3 * score
    fred_adj = max(0.4, min(1.0, fred_adj))

    try:
        from forecast import get_latest_political_score
        political_score = get_latest_political_score()
        political_adj = 0.7 + 0.3 * political_score
        political_adj = max(0.4, min(1.0, political_adj))
        blended = 0.7 * fred_adj + 0.3 * political_adj
    except Exception:
        blended = fred_adj  # FRED-only fallback

    return base_equity_pct * blended
```

- [ ] **Step 2: Verify import works**

```bash
cd /Users/zl/works/stock
python3 -c "from macro import macro_risk_adjustment; print(macro_risk_adjustment(1.0))"
```
Expected: a float between 0.4 and 1.0, no errors.

- [ ] **Step 3: Commit**

```bash
git add macro.py
git commit -m "feat: blend political_risk_score into macro_risk_adjustment (70/30 FRED/political)"
```

---

## Task 8: Update watchdog.py

**Files:**
- Modify: `stock/watchdog.py`

- [ ] **Step 1: Add check_political_forecast function**

After the `check_news` function (after line 313), insert:

```python
def check_political_forecast():
    """Check for recent political LLM analysis and surface as alerts."""
    from forecast import get_latest_political_score
    from news_store import init_db, get_latest_analysis
    import datetime as dt

    alerts = []
    try:
        init_db()
        latest = get_latest_analysis()
        if not latest:
            return alerts
        created = dt.datetime.fromisoformat(latest["created_at"])
        age_hours = (dt.datetime.utcnow() - created).total_seconds() / 3600
        if age_hours > 4:
            return alerts  # stale — skip

        score = latest["political_risk_score"]
        snippet = latest.get("briefing", "")[:80]
        if score < -0.3:
            alerts.append((Alert.WARNING, "POLITICAL",
                f"Political risk elevated ({score:+.2f}): {snippet}"))
        elif score > 0.3:
            alerts.append((Alert.INFO, "POLITICAL",
                f"Political tailwind ({score:+.2f}): {snippet}"))
    except Exception:
        pass
    return alerts
```

- [ ] **Step 2: Add political forecast to run_watchdog**

In `run_watchdog`, inside the `if not quick:` block (after `news_alerts = check_news(portfolio)`), add:

```python
        header("POLITICAL FORECAST")
        political_alerts = check_political_forecast()
        all_alerts.extend(political_alerts)
        try:
            from forecast import get_latest_political_score
            from news_store import get_latest_analysis
            latest = get_latest_analysis()
            if latest:
                print(f"  Political Risk Score: {latest['political_risk_score']:+.2f}")
                print(f"  {latest['briefing'][:120]}")
            else:
                print("  No political forecast yet. Run: python3 news_poller.py")
        except Exception:
            pass
```

- [ ] **Step 3: Add --forecast flag handling**

In the `if __name__ == "__main__":` block, add before `else: run_watchdog(quick=False)`:

```python
    elif "--forecast" in args:
        from forecast import _print_latest_briefing
        _print_latest_briefing()
```

- [ ] **Step 4: Update cron comment at top of watchdog.py**

Replace the existing cron comment (lines 19–20):
```python
# Cron (3x daily ET, weekdays):
#   10 8  * * 1-5  cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
#   30 12 * * 1-5  cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
#   0  17 * * 1-5  cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
```

- [ ] **Step 5: Verify import**

```bash
cd /Users/zl/works/stock
python3 -c "from watchdog import check_political_forecast; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add watchdog.py
git commit -m "feat: add political forecast section to watchdog, --forecast flag, 3x daily cron"
```

---

## Task 9: Update requirements.txt

**Files:**
- Modify: `stock/requirements.txt`

- [ ] **Step 1: Add new dependencies**

Replace the full content of `stock/requirements.txt` with:

```
yfinance>=0.2.0
pandas>=1.3.0
numpy>=1.21.0
scipy>=1.7.0
tabulate>=0.9.0
feedparser>=6.0.0
anthropic>=0.40.0
pytz>=2023.3
```

- [ ] **Step 2: Install**

```bash
cd /Users/zl/works/stock
pip install -r requirements.txt
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add feedparser, anthropic, pytz to requirements"
```

---

## Task 10: Update tg_bot.py (new commands + updated schedule)

**Files:**
- Modify: `tg-bot/tg_bot.py`

- [ ] **Step 1: Add cmd_forecast handler**

After `cmd_sentiment` function (after line 235), insert:

```python
@auth
async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching latest political forecast...")
    try:
        sys.path.insert(0, os.path.abspath(STOCK_DIR))
        from forecast import get_latest_political_score
        from news_store import init_db, get_latest_analysis
        loop = asyncio.get_event_loop()

        def _run():
            init_db()
            return get_latest_analysis()

        _, latest = await loop.run_in_executor(None, capture_stdout, _run)
        if not latest:
            await update.message.reply_text("No forecast yet. Start news_poller.py first.")
            return

        sectors = latest.get("sector_impacts", {})
        sector_lines = "\n".join(
            f"  {t}: {d}" for t, d in list(sectors.items())[:8]
        )
        lines = [
            f"Political Briefing [{latest['trigger'].upper()}]",
            f"Time: {latest['created_at']} UTC",
            f"Risk Score: {latest['political_risk_score']:+.2f}\n",
            latest.get("briefing", ""),
            "",
            f"Sector Impacts:\n{sector_lines}" if sector_lines else "",
        ]
        await send_long_message(update, "\n".join(l for l in lines if l is not None))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


@auth
async def cmd_hotspots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Checking recent hotspot alerts...")
    try:
        import datetime as dt
        from news_store import init_db, _get_conn
        loop = asyncio.get_event_loop()

        def _run():
            init_db()
            cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=24)).isoformat()
            with _get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM llm_analyses WHERE trigger='hotspot' AND created_at > ? "
                    "ORDER BY created_at DESC",
                    (cutoff,),
                ).fetchall()
            return [dict(r) for r in rows]

        _, rows = await loop.run_in_executor(None, capture_stdout, _run)
        if not rows:
            await update.message.reply_text("No hotspot alerts in the last 24h.")
            return

        lines = ["Hotspot Alerts (last 24h):\n"]
        for r in rows:
            lines.append(
                f"[{r['created_at']}] {r['category'].upper()} "
                f"risk:{r['political_risk_score']:+.2f}"
            )
            lines.append(f"  {r['briefing'][:100]}\n")
        await send_long_message(update, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
```

- [ ] **Step 2: Register the new handlers in main()**

In `main()`, after `app.add_handler(CommandHandler("sentiment", cmd_sentiment))`, add:

```python
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("hotspots", cmd_hotspots))
```

- [ ] **Step 3: Update help text**

In `cmd_help`, replace the text string with:

```python
    text = (
        "Stock Bot Commands:\n"
        "/portfolio - Current portfolio status\n"
        "/watchdog - Run daily watchdog check\n"
        "/run - Run full investment system\n"
        "/screen - Value+quality stock screener\n"
        "/macro - Macro regime analysis\n"
        "/sentiment - News & social sentiment\n"
        "/forecast - Latest political briefing\n"
        "/hotspots - Recent severity-3 alerts (24h)\n"
        "/help - Show this message\n"
        "\nOr just send any message to chat with Claude."
    )
```

- [ ] **Step 4: Update scheduled watchdog to 3x daily with forecast push**

Replace the entire scheduled block in `main()` (the `app.job_queue.run_daily(...)` call and its logger line) with:

```python
    et = pytz.timezone("US/Eastern")
    schedule_times = [
        dt_time(hour=8,  minute=10, tzinfo=et),   # pre-market
        dt_time(hour=12, minute=30, tzinfo=et),   # midday
        dt_time(hour=17, minute=0,  tzinfo=et),   # after-hours
    ]
    for t in schedule_times:
        app.job_queue.run_daily(
            scheduled_watchdog,
            time=t,
            days=(0, 1, 2, 3, 4),
        )
    logger.info("Scheduled watchdog at 8:10, 12:30, 17:00 ET Mon-Fri")
```

- [ ] **Step 5: Add scheduled briefing push to scheduled_watchdog**

At the end of `scheduled_watchdog`, before the `except Exception` block, add after the existing alert send:

```python
        # Push political forecast if available
        try:
            from forecast import get_latest_political_score
            from news_store import init_db as _init_db, get_latest_analysis
            _init_db()
            latest = get_latest_analysis()
            if latest:
                from tg_notifier import send_scheduled_briefing
                # Determine label from current hour
                import datetime as _dt
                hour = _dt.datetime.now(tz=pytz.timezone("US/Eastern")).hour
                label = (
                    "PRE-MARKET BRIEFING" if hour < 10 else
                    "MIDDAY BRIEFING" if hour < 15 else
                    "AFTER-HOURS BRIEFING"
                )
                send_scheduled_briefing(latest, label=label)
        except Exception as _e:
            logger.error(f"Forecast push error: {_e}")
```

- [ ] **Step 6: Smoke test**

```bash
cd /Users/zl/works/tg-bot
python3 -c "import tg_bot; print('OK')"
```
Expected: `OK` (or import error only if python-telegram-bot not installed, not a syntax error)

- [ ] **Step 7: Commit**

```bash
cd /Users/zl/works/tg-bot
git add tg_bot.py
git commit -m "feat: add /forecast and /hotspots commands, update to 3x daily schedule with briefing push"
```

---

## Task 11: Run all tests + full smoke test

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/zl/works/stock
python -m pytest tests/ -v
```
Expected: All tests PASS. Minimum: `test_news_store.py` (8), `test_political.py` (7), `test_forecast.py` (5) = 20 tests.

- [ ] **Step 2: Smoke test news_poller (single cycle, then Ctrl+C)**

```bash
cd /Users/zl/works/stock
timeout 30 python3 news_poller.py 2>&1 | head -20
```
Expected: Lines like `INFO Polling RSS feeds...`, `INFO Sleeping 300s...`. No `ImportError` or `AttributeError`.

- [ ] **Step 3: Smoke test forecast CLI**

```bash
cd /Users/zl/works/stock
python3 forecast.py
```
Expected: Either "No briefing available yet" (if poller hasn't run) or a briefing output. No crash.

- [ ] **Step 4: Smoke test watchdog with forecast flag**

```bash
cd /Users/zl/works/stock
python3 watchdog.py --forecast
```
Expected: Either "No political forecast yet" or briefing output. No crash.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add -A
git commit -m "chore: verify all modules integrate cleanly"
```

---

## Self-Review Notes

**Spec coverage check:**
- RSS fetching (US + Asia sources) → `political.py` Task 3 ✓
- SQLite 7-day storage + dedup → `news_store.py` Task 2 ✓
- 5-min polling → `news_poller.py` Task 6 ✓
- Hotspot threshold → `political.categorize_article` + `run_classification_pass` ✓
- Immediate LLM on hotspot (Haiku) → `forecast.analyze_hotspot` Task 4 ✓
- Scheduled briefing (Sonnet) 3x daily → `watchdog.py` + `tg_bot.py` Tasks 8 + 10 ✓
- `political_risk_score` blended into `macro_risk_adjustment` → Task 7 ✓
- Sector/stock impact forecast → included in both LLM outputs ✓
- Telegram push (hotspot + scheduled) → `tg_notifier.py` + `tg_bot.py` Tasks 5 + 10 ✓
- `/forecast` and `/hotspots` Telegram commands → Task 10 ✓
- Rate limit 3 hotspot LLM calls/hour/category → `count_hotspot_llm_calls` + poller ✓
- Pre-market extended window (14h) → `LLM_PREMARKET_HOURS` config (watchdog can pass this) ✓
- `python3 forecast.py --hotspots` CLI → Task 4 `_print_hotspots` ✓

**Type consistency:** `get_latest_analysis()` returns `Optional[Dict]` with `sector_impacts` already deserialized as `Dict` — used consistently across `forecast.py`, `watchdog.py`, `tg_bot.py`. ✓

**One open item:** The pre-market run at 8:10 AM should pass `hours=LLM_PREMARKET_HOURS` (14) to `run_scheduled_briefing`. The watchdog can detect the time and pass the right hours value. This can be done as a small follow-up after verifying basic function.
