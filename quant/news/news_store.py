"""
SQLite-backed news article storage.
7-day retention, URL dedup, hotspot queries.
"""
import os
from quant import paths
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# Allow override for tests — read at connection time so env changes take effect
_DEFAULT_DB_PATH = os.path.join(paths.CACHE_DIR, "news.db")


def _get_conn() -> sqlite3.Connection:
    db_path = os.environ.get("NEWS_DB_PATH", _DEFAULT_DB_PATH)
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
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
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO events (article_id, category, keywords, severity, created_at) "
                "VALUES (?,?,?,?,?)",
                (article_id, category, json.dumps(keywords), severity, now),
            )
    except sqlite3.Error as e:
        import sys
        print(f"news_store: insert_event error: {e}", file=sys.stderr)


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
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO llm_analyses "
                "(trigger, category, input_summary, briefing, sector_impacts, political_risk_score, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (trigger, category, input_summary, briefing,
                 json.dumps(sector_impacts), political_risk_score, now),
            )
    except sqlite3.Error as e:
        import sys
        print(f"news_store: insert_analysis error: {e}", file=sys.stderr)


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
               GROUP BY a.id
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
        conn.execute("DELETE FROM llm_analyses WHERE created_at < ?", (cutoff,))


def count_hotspot_llm_calls(category: str, hours: int = 1) -> int:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM llm_analyses "
            "WHERE trigger='hotspot' AND category=? AND created_at > ?",
            (category, cutoff),
        ).fetchone()
        return row[0]
