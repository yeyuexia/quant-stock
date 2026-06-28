#!/usr/bin/env python3
"""
News polling daemon.

Fetches RSS feeds every 5 minutes, classifies events, detects hotspots,
triggers LLM analysis and Telegram push on severity-3 events.

Also runs scheduled briefings at 8:10, 12:30, 17:00 ET (Mon-Fri)
and pushes them directly to Telegram — no tg_bot.py required.

Run: python3 news_poller.py
"""
import sys
import os
import time
import logging
import datetime as dt

import pytz


from quant.news.news_store import init_db, cleanup_old_articles, count_hotspot_llm_calls
from quant.news.political import run_classification_pass
from quant.news.forecast import analyze_hotspot, run_scheduled_briefing
from quant.infra.tg_notifier import send_hotspot_alert, send_scheduled_briefing
from quant.config import (
    NEWS_POLL_INTERVAL_SECONDS,
    NEWS_RETENTION_DAYS,
    HOTSPOT_MAX_LLM_CALLS_PER_HOUR,
    LLM_PREMARKET_HOURS,
    LLM_BRIEFING_HOURS,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

# Scheduled briefing times (hour, minute) in ET, weekdays only
BRIEFING_SCHEDULE = [
    (8,  10, "PRE-MARKET BRIEFING",    LLM_PREMARKET_HOURS),
    (12, 30, "MIDDAY BRIEFING",        LLM_BRIEFING_HOURS),
    (17,  0, "AFTER-HOURS BRIEFING",   LLM_BRIEFING_HOURS),
]

# Tracks which briefing slots have already fired today
_fired_today: set = set()


def _check_scheduled_briefing():
    """Fire a scheduled briefing if the current time matches a slot."""
    global _fired_today
    now = dt.datetime.now(tz=ET)

    # Reset fired slots at midnight
    today_key = now.date().isoformat()
    _fired_today = {k for k in _fired_today if k.startswith(today_key)}

    # Only run on weekdays
    if now.weekday() >= 5:
        return

    for hour, minute, label, hours in BRIEFING_SCHEDULE:
        slot_key = f"{today_key}-{hour:02d}{minute:02d}"
        if slot_key in _fired_today:
            continue

        # Fire if we're within the 5-minute poll window of the target time
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = abs((now - target).total_seconds())
        if delta <= NEWS_POLL_INTERVAL_SECONDS:
            logger.info("Scheduled briefing: %s", label)
            _fired_today.add(slot_key)
            try:
                result = run_scheduled_briefing(hours=hours)
                send_scheduled_briefing(result, label=label)
                logger.info("Briefing sent: %s (risk score: %+.2f)",
                            label, result.get("political_risk_score", 0.0))
            except Exception as e:
                logger.error("Scheduled briefing failed (%s): %s", label, e)


def poll_once():
    logger.info("Polling RSS feeds...")
    hotspots = run_classification_pass()

    for hotspot in hotspots:
        cat = hotspot["category"]
        count = hotspot["count"]
        articles = hotspot["articles"]

        if count_hotspot_llm_calls(cat, hours=1) >= HOTSPOT_MAX_LLM_CALLS_PER_HOUR:
            logger.info("Rate limit reached for %s, skipping LLM call", cat)
            continue

        logger.info("Hotspot: %s (%d events in window) — calling LLM", cat, count)
        try:
            analysis = analyze_hotspot(cat, articles)
            send_hotspot_alert(cat, analysis)
            logger.info("Hotspot alert sent for %s", cat)
        except Exception as e:
            logger.error("LLM hotspot analysis failed for %s: %s", cat, e)

    cleanup_old_articles(days=NEWS_RETENTION_DAYS)
    _check_scheduled_briefing()


def main():
    logger.info("News poller starting...")
    init_db()
    while True:
        try:
            poll_once()
        except Exception as e:
            logger.error("Poll error: %s", e)
        logger.info("Sleeping %ds...", NEWS_POLL_INTERVAL_SECONDS)
        time.sleep(NEWS_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
