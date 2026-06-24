"""News-keyword detector that feeds circuit breaker D.

Public functions:
  fetch_recent_headlines(since) -> list[dict]  (live — RSS + Reddit)
  match_headlines(headlines, keywords, plan_symbols) -> list[NewsHit]
  dedupe_by_title_hash(hits, window_minutes) -> list[NewsHit]
  log_hit(hit, corroborated) -> None

Breaker D (in breakers.py) composes these: fetch → match → dedupe →
check SPY corroboration → log → return BreakerResult.
"""
from __future__ import annotations
import csv
import datetime as dt
import fcntl
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable

import config

_log = logging.getLogger(__name__)

NEWS_SHOCK_LOG = config.NEWS_SHOCK_LOG


@dataclass(frozen=True)
class NewsHit:
    title: str
    source: str
    ts: dt.datetime
    matched: str


def match_headlines(
    headlines: Iterable[dict],
    keywords: Iterable[str],
    plan_symbols: set,
) -> list:
    kw_list = [k.lower() for k in keywords]
    hits: list = []
    for h in headlines:
        title = h["title"]
        title_lc = title.lower()

        matched_kw = None
        for k in kw_list:
            if " " in k:
                if k in title_lc:
                    matched_kw = k
                    break
            else:
                if re.search(rf"\b{re.escape(k)}\b", title_lc):
                    matched_kw = k
                    break

        matched_ticker = None
        for sym in plan_symbols:
            if re.search(rf"\b{re.escape(sym)}\b", title):
                matched_ticker = sym
                break

        if matched_ticker:
            hits.append(NewsHit(title=title, source=h["source"], ts=h["ts"],
                                matched=matched_ticker))
        elif matched_kw:
            hits.append(NewsHit(title=title, source=h["source"], ts=h["ts"],
                                matched=matched_kw))
    return hits


def dedupe_by_title_hash(hits: Iterable, window_minutes: int) -> list:
    seen = {}
    out = []
    window = dt.timedelta(minutes=window_minutes)
    for h in sorted(hits, key=lambda x: x.ts):
        th = _title_hash(h.title)
        prior = seen.get(th)
        if prior is None or (h.ts - prior) > window:
            out.append(h)
            seen[th] = h.ts
    return out


def log_hit(hit: NewsHit, corroborated: bool) -> None:
    """Append a news-hit observation to the CSV audit log.

    Lock-protected (fcntl on `.lock` sidecar) so concurrent ticks across
    executor + watchdog can't garble each other's rows mid-flush. Header
    is written exactly once on first append.
    """
    parent = os.path.dirname(NEWS_SHOCK_LOG)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lock_path = NEWS_SHOCK_LOG + ".lock"
    with open(lock_path, "w") as lk:
        try:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass  # filesystem doesn't support locking — fall through
        exists = os.path.exists(NEWS_SHOCK_LOG)
        with open(NEWS_SHOCK_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["ts", "source", "matched", "corroborated", "title"])
            w.writerow([hit.ts.isoformat(), hit.source, hit.matched,
                        corroborated, hit.title])


def fetch_recent_headlines(since: dt.datetime) -> list:
    """Pull headlines from Yahoo Finance + Reddit since cursor.

    Returns list of {"title": str, "source": str, "ts": datetime}. Best-effort:
    if either feed errors, return what we got from the other. Never raises.
    """
    out = []
    out.extend(_fetch_yahoo_headlines(since))
    out.extend(_fetch_reddit_headlines(since))
    return out


def _fetch_yahoo_headlines(since: dt.datetime) -> list:
    try:
        import yfinance as yf
        news = yf.Ticker("^GSPC").news or []
    except Exception as e:
        # Silent failure used to make a broken yfinance .news endpoint
        # impossible to detect — breaker D would just never fire. Log it.
        _log.warning("yahoo headlines fetch failed: %s", e)
        return []
    out = []
    for n in news:
        ts = dt.datetime.fromtimestamp(
            n.get("providerPublishTime", 0), tz=dt.timezone.utc,
        )
        if ts < since:
            continue
        out.append({
            "title": n.get("title", ""),
            "source": n.get("publisher", "yahoo"),
            "ts": ts,
        })
    return out


def _fetch_reddit_headlines(since: dt.datetime) -> list:
    try:
        import urllib.request
        import json
        req = urllib.request.Request(
            "https://www.reddit.com/r/stocks/hot.json?limit=25",
            headers={"User-Agent": "stock-tracker/1.0 (research)"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
    except Exception as e:
        _log.warning("reddit headlines fetch failed: %s", e)
        return []
    out = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        ts = dt.datetime.fromtimestamp(d.get("created_utc", 0), tz=dt.timezone.utc)
        if ts < since:
            continue
        out.append({
            "title": d.get("title", ""),
            "source": "reddit/stocks",
            "ts": ts,
        })
    return out


def _title_hash(title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.lower().strip())
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]


def title_hash(title: str) -> str:
    """Public wrapper for the title fingerprint used by dedupe."""
    return _title_hash(title)
