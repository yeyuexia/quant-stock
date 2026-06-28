"""
RSS news fetching and rule-based political event classification.

Fetches configured RSS feeds, classifies articles into event categories,
detects hotspots (volume spikes within a time window).
"""
import time
import re
import logging
import urllib.request
from typing import List, Dict, Tuple, Optional

import feedparser

logger = logging.getLogger(__name__)

from quant.config import RSS_FEEDS, NEWS_CATEGORIES, HOTSPOT_WINDOW_MINUTES, HOTSPOT_THRESHOLD_COUNT
from quant.news.news_store import (
    insert_article, insert_event,
    count_events_in_window, get_recent_events,
)


def fetch_rss_feed(url: str, source: str, region: str) -> List[Dict]:
    """Fetch one RSS feed. Returns list of article dicts."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            feed = feedparser.parse(response)
        if feed.get("bozo") and not feed.entries:
            logger.warning("Malformed feed from %s (%s): %s", source, url, feed.get("bozo_exception"))
            return []
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
    except Exception as e:
        logger.warning("Failed to fetch feed %s (%s): %s", source, url, e)
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

    for cat, cfg in NEWS_CATEGORIES.items():
        matched_kws = []
        for kw in cfg["keywords"]:
            # Use word boundaries to avoid matching substrings like "war" in "award"
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                matched_kws.append(kw)
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
    within HOTSPOT_WINDOW_MINUTES.

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
    for cat in NEWS_CATEGORIES:
        count = count_events_in_window(cat, HOTSPOT_WINDOW_MINUTES)
        if count >= HOTSPOT_THRESHOLD_COUNT:
            recent = get_recent_events(cat, HOTSPOT_WINDOW_MINUTES)
            hotspots.append({"category": cat, "count": count, "articles": recent})

    return hotspots
