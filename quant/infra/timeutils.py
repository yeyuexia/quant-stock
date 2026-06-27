"""Time/zone utilities shared across watchdog, executor, etc.

US/Eastern wall clock + RTH gate. All consumers should import from here
rather than reimplementing the zoneinfo dance.
"""
from __future__ import annotations
import datetime as dt


def now_et() -> dt.datetime:
    """US/Eastern wall clock (naive). Raises on environment failure rather
    than silently falling back to a wrong timezone — caller decides how to
    handle (we don't want to ship orders against the wrong clock half the year).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError as e:
        raise RuntimeError(
            "zoneinfo not available — install Python ≥3.9 or backport tzdata"
        ) from e
    return dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)


def is_rth_now() -> bool:
    """True iff NY time is within 09:30–16:00 on a weekday.

    Does NOT account for US market holidays — callers that need that should
    still verify with broker.is_market_open() on day boundaries.
    """
    now = now_et()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now < close_t
