"""
News & Social Sentiment Monitor

Free data sources:
  1. Yahoo Finance news (via yfinance) — per-ticker and market-wide
  2. Reddit (JSON API, no auth) — r/wallstreetbets, r/stocks, r/investing
  3. RSS feeds — CNBC, Reuters, Bloomberg (headlines)

Produces:
  - Trending tickers and topics
  - Sentiment scoring (bullish/bearish/neutral)
  - Hotspot alerts that may impact our portfolio
"""
import os
from quant import paths
import json
import logging
import time
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from quant.infra.fileio import atomic_write_json

_log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(paths.REPO_ROOT, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Sentiment keywords ──────────────────────────────────────────
BULLISH_WORDS = {
    "bull", "bullish", "buy", "calls", "moon", "rocket", "squeeze",
    "breakout", "upgrade", "beat", "surge", "rally", "soar", "boom",
    "record", "high", "growth", "profit", "earnings beat", "upside",
    "oversold", "undervalued", "accumulate", "long", "rip", "tendies",
    "diamond hands", "hold", "strong", "recovery", "rebound",
}

BEARISH_WORDS = {
    "bear", "bearish", "sell", "puts", "crash", "dump", "tank",
    "breakdown", "downgrade", "miss", "plunge", "drop", "fall", "sink",
    "recession", "low", "loss", "debt", "default", "overvalued",
    "overbought", "short", "rug pull", "bubble", "collapse", "panic",
    "fear", "layoff", "cut", "warning", "risk", "tariff", "war",
}

# Known ticker patterns (avoid false positives like "A", "IT", "ALL")
TICKER_PATTERN = re.compile(r'\$([A-Z]{1,5})\b')
TICKER_MENTION = re.compile(r'\b([A-Z]{2,5})\b')

# Tickers we care about (from our universe)
from quant.config import ETF_UNIVERSE, WATCHLIST, SAFE_HAVEN
OUR_TICKERS = set(ETF_UNIVERSE + WATCHLIST + [SAFE_HAVEN])

# Common words to exclude from ticker detection
NOT_TICKERS = {
    "CEO", "CFO", "CTO", "IPO", "ETF", "GDP", "CPI", "FBI", "SEC",
    "FDA", "FED", "IMF", "NYSE", "THE", "AND", "FOR", "ARE", "BUT",
    "NOT", "YOU", "ALL", "CAN", "HER", "WAS", "ONE", "OUR", "OUT",
    "HAS", "HIS", "HOW", "ITS", "MAY", "NEW", "NOW", "OLD", "SEE",
    "WAY", "WHO", "DID", "GOT", "LET", "SAY", "SHE", "TOO", "USE",
    "HIM", "HAD", "RUN", "BIG", "TOP", "LOW", "HIGH", "USD", "AI",
    "US", "UK", "EU", "PM", "AM", "DD", "YOLO", "HODL", "FOMO",
    "PSA", "TIL", "IMO", "FYI", "LOL", "OMG", "WTF", "EDIT",
    "POST", "JUST", "LIKE", "THIS", "THAT", "WITH", "FROM",
    "WHAT", "WHEN", "WILL", "BEEN", "HAVE", "EACH", "MAKE",
    "VERY", "MUCH", "SOME", "ONLY", "ALSO", "BACK", "MOST",
}


def _cache_get(key: str, ttl_minutes: int = 30):
    path = os.path.join(CACHE_DIR, f"sentiment_{key}.json")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl_minutes * 60:
            with open(path) as f:
                return json.load(f)
    return None


def _cache_set(key: str, data):
    """Lock-protected cache write — concurrent watchdog + run.py shouldn't
    race on the sentiment cache."""
    path = os.path.join(CACHE_DIR, f"sentiment_{key}.json")
    try:
        atomic_write_json(path, data)
    except Exception as e:
        _log.warning("sentiment cache write failed for %s: %s", key, e)


# ── Yahoo Finance News ──────────────────────────────────────────

def fetch_yf_news(tickers=None) -> List[dict]:
    """Fetch news from Yahoo Finance via yfinance."""
    import yfinance as yf

    cached = _cache_get("yf_news", ttl_minutes=30)
    if cached:
        return cached

    all_news = []
    targets = tickers or ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL"]

    for t in targets:
        try:
            ticker = yf.Ticker(t)
            news = ticker.news
            if not news:
                continue
            for item in news[:5]:
                content = item.get("content", {})
                all_news.append({
                    "source": "Yahoo Finance",
                    "ticker": t,
                    "title": content.get("title", ""),
                    "summary": content.get("summary", ""),
                    "link": content.get("canonicalUrl", {}).get("url", ""),
                    "published": content.get("pubDate", ""),
                    "provider": content.get("provider", {}).get("displayName", ""),
                })
        except Exception as e:
            _log.warning("fetch_yf_news: %s failed: %s", t, e)
            continue

    # Deduplicate by title
    seen = set()
    unique = []
    for n in all_news:
        if n["title"] and n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    _cache_set("yf_news", unique)
    return unique


# ── Reddit ──────────────────────────────────────────────────────

def fetch_reddit_posts(subreddit: str, limit: int = 25) -> List[dict]:
    """Fetch top/hot posts from a subreddit using the JSON API (no auth)."""
    import urllib.request

    cache_key = f"reddit_{subreddit}"
    cached = _cache_get(cache_key, ttl_minutes=30)
    if cached:
        return cached

    url = f"https://old.reddit.com/r/{subreddit}/hot.json?limit={limit}&raw_json=1"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        _log.warning("fetch_reddit_posts: r/%s failed: %s", subreddit, e)
        return []

    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        posts.append({
            "source": f"r/{subreddit}",
            "title": d.get("title", ""),
            "selftext": (d.get("selftext", "") or "")[:500],
            "score": d.get("score", 0),
            "num_comments": d.get("num_comments", 0),
            "url": f"https://reddit.com{d.get('permalink', '')}",
            "created": datetime.fromtimestamp(d.get("created_utc", 0)).isoformat(),
            "flair": d.get("link_flair_text", ""),
        })

    _cache_set(cache_key, posts)
    return posts


def fetch_all_reddit() -> List[dict]:
    """Fetch from all relevant finance subreddits."""
    subreddits = ["wallstreetbets", "stocks", "investing", "stockmarket"]
    all_posts = []
    for sub in subreddits:
        posts = fetch_reddit_posts(sub, limit=25)
        all_posts.extend(posts)
    return all_posts


# ── Sentiment Analysis ──────────────────────────────────────────

def _word_or_phrase_count(text_lower: str, vocabulary) -> int:
    """Count occurrences of words/phrases in `vocabulary` within `text_lower`.
    Single-word entries match on \\b word boundary so 'bear' doesn't fire on
    'bearable' (substring match was the old behavior). Multi-word entries
    fall back to substring `in` since they're already specific.
    """
    n = 0
    for term in vocabulary:
        if " " in term:
            if term in text_lower:
                n += 1
        else:
            if re.search(rf"\b{re.escape(term)}\b", text_lower):
                n += 1
    return n


def _score_text(text: str) -> Tuple[float, str]:
    """Simple keyword-based sentiment scoring.
    Returns (score, label) where score is -1 to +1."""
    text_lower = text.lower()

    bull_count = _word_or_phrase_count(text_lower, BULLISH_WORDS)
    bear_count = _word_or_phrase_count(text_lower, BEARISH_WORDS)

    total = bull_count + bear_count
    if total == 0:
        return 0.0, "neutral"

    score = (bull_count - bear_count) / total
    if score > 0.2:
        label = "bullish"
    elif score < -0.2:
        label = "bearish"
    else:
        label = "neutral"

    return score, label


def _extract_tickers(text: str) -> List[str]:
    """Extract stock ticker mentions from text."""
    tickers = set()

    # $TICKER format (high confidence)
    for match in TICKER_PATTERN.finditer(text):
        t = match.group(1)
        if t not in NOT_TICKERS:
            tickers.add(t)

    # TICKER format (only if it's in our universe)
    for match in TICKER_MENTION.finditer(text):
        t = match.group(1)
        if t in OUR_TICKERS:
            tickers.add(t)

    return list(tickers)


def analyze_news_sentiment(news: List[dict]) -> dict:
    """Analyze sentiment of news articles."""
    results = []
    for n in news:
        text = f"{n.get('title', '')} {n.get('summary', '')}"
        score, label = _score_text(text)
        tickers = _extract_tickers(text)
        results.append({
            **n,
            "sentiment_score": score,
            "sentiment": label,
            "mentioned_tickers": tickers,
        })

    return results


def analyze_reddit_sentiment(posts: List[dict]) -> List[dict]:
    """Analyze sentiment of Reddit posts."""
    results = []
    for p in posts:
        text = f"{p.get('title', '')} {p.get('selftext', '')}"
        score, label = _score_text(text)
        tickers = _extract_tickers(text)
        # Weight by engagement
        engagement = p.get("score", 0) + p.get("num_comments", 0) * 2
        results.append({
            **p,
            "sentiment_score": score,
            "sentiment": label,
            "mentioned_tickers": tickers,
            "engagement": engagement,
        })

    # Sort by engagement
    results.sort(key=lambda x: x["engagement"], reverse=True)
    return results


# ── Aggregation ─────────────────────────────────────────────────

def get_ticker_buzz() -> pd.DataFrame:
    """Aggregate ticker mentions and sentiment across all sources."""
    news = analyze_news_sentiment(fetch_yf_news())
    reddit = analyze_reddit_sentiment(fetch_all_reddit())

    ticker_data = {}  # ticker -> {mentions, sentiment_sum, sources, headlines}

    for item in news + reddit:
        for t in item.get("mentioned_tickers", []):
            if t not in ticker_data:
                ticker_data[t] = {
                    "mentions": 0,
                    "sentiment_sum": 0,
                    "bullish": 0,
                    "bearish": 0,
                    "neutral": 0,
                    "headlines": [],
                    "sources": set(),
                }
            d = ticker_data[t]
            d["mentions"] += 1
            d["sentiment_sum"] += item.get("sentiment_score", 0)
            d[item.get("sentiment", "neutral")] += 1
            d["sources"].add(item.get("source", ""))
            title = item.get("title", "")
            if title and len(d["headlines"]) < 3:
                d["headlines"].append(title[:80])

    rows = []
    for t, d in ticker_data.items():
        avg_sent = d["sentiment_sum"] / d["mentions"] if d["mentions"] > 0 else 0
        rows.append({
            "ticker": t,
            "mentions": d["mentions"],
            "avg_sentiment": avg_sent,
            "bullish": d["bullish"],
            "bearish": d["bearish"],
            "neutral": d["neutral"],
            "sources": len(d["sources"]),
            "in_portfolio": t in OUR_TICKERS,
            "top_headline": d["headlines"][0] if d["headlines"] else "",
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("mentions", ascending=False)
    return df


def get_market_hotspots() -> dict:
    """Identify market hotspots — trending topics that may impact our portfolio.

    Returns:
      - trending_tickers: top mentioned tickers
      - portfolio_alerts: news/sentiment affecting our holdings
      - market_mood: overall market sentiment
      - top_stories: most impactful headlines
      - reddit_buzz: hottest Reddit discussions
    """
    news = analyze_news_sentiment(fetch_yf_news())
    reddit = analyze_reddit_sentiment(fetch_all_reddit())

    # Overall market mood
    all_scores = [x["sentiment_score"] for x in news + reddit if x["sentiment_score"] != 0]
    if all_scores:
        market_mood = sum(all_scores) / len(all_scores)
    else:
        market_mood = 0

    if market_mood > 0.2:
        mood_label = "BULLISH"
    elif market_mood < -0.2:
        mood_label = "BEARISH"
    else:
        mood_label = "NEUTRAL"

    # Portfolio-specific alerts
    portfolio_alerts = []
    for item in news + reddit:
        for t in item.get("mentioned_tickers", []):
            if t in OUR_TICKERS:
                portfolio_alerts.append({
                    "ticker": t,
                    "headline": item.get("title", "")[:100],
                    "sentiment": item.get("sentiment", "neutral"),
                    "source": item.get("source", ""),
                    "engagement": item.get("engagement", item.get("score", 0)),
                })

    # Deduplicate alerts by headline
    seen = set()
    unique_alerts = []
    for a in portfolio_alerts:
        if a["headline"] not in seen:
            seen.add(a["headline"])
            unique_alerts.append(a)
    portfolio_alerts = sorted(unique_alerts, key=lambda x: x.get("engagement", 0), reverse=True)

    # Top stories (highest engagement from Reddit)
    top_reddit = reddit[:10]

    # Trending topics from Reddit (keywords)
    all_text = " ".join(p.get("title", "") for p in reddit)
    topic_words = re.findall(r'\b[A-Za-z]{4,}\b', all_text.lower())
    # Filter out very common words
    common = {"this", "that", "with", "from", "what", "when", "will", "have",
              "been", "just", "like", "more", "than", "them", "they", "your",
              "about", "would", "could", "should", "into", "some", "after",
              "before", "other", "over", "even", "still", "here", "there",
              "where", "these", "those", "being", "going", "doing", "make",
              "made", "think", "know", "need", "want", "good", "best", "next",
              "last", "much", "most", "very", "down", "year", "years", "today",
              "people", "time", "market", "stock", "stocks", "money", "price",
              "share", "shares"}
    topic_counts = Counter(w for w in topic_words if w not in common)

    return {
        "market_mood": market_mood,
        "mood_label": mood_label,
        "portfolio_alerts": portfolio_alerts[:15],
        "top_reddit": top_reddit,
        "trending_topics": topic_counts.most_common(15),
        "ticker_buzz": get_ticker_buzz(),
        "news_count": len(news),
        "reddit_count": len(reddit),
    }
