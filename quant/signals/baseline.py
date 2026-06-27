# baseline.py
"""Captures plan-time market snapshots that circuit breakers diff against.

All fetchers are private functions to make the public surface easy to mock
in tests: just patch _fetch_spy / _fetch_vix / _fetch_macro_score.
"""
from __future__ import annotations
import datetime as dt
import logging
import os
import time
from typing import Optional

from quant.execution.pending_plan import Baseline

_log = logging.getLogger(__name__)

_RETRY_BACKOFF_MS = 300

# Module-level market-data client — reused across capture_baseline calls
# instead of paying a TLS handshake per fetcher invocation. Lazily created
# so importing baseline.py doesn't require Alpaca creds (tests).
_MD_CLIENT = None


def _md_client():
    global _MD_CLIENT
    if _MD_CLIENT is None:
        from alpaca.data.historical import StockHistoricalDataClient
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        if not key or not secret:
            raise RuntimeError(
                "baseline._fetch_spy: ALPACA_API_KEY/SECRET not set"
            )
        _MD_CLIENT = StockHistoricalDataClient(api_key=key, secret_key=secret)
    return _MD_CLIENT


def _retry(fn, attempts: int = 2):
    """Call fn() with up to `attempts` total tries. Brief sleep between."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(_RETRY_BACKOFF_MS / 1000.0)
    raise last_exc  # type: ignore[misc]


def capture_baseline() -> Baseline:
    """Snapshot SPY / VIX / macro at plan-creation time.

    Each value is validated before returning — circuit breakers divide
    by baseline.spy and baseline.vix, so a 0 or NaN here would crash the
    executor downstream. Raising early gives a clearer failure mode.
    """
    spy = _fetch_spy()
    vix = _fetch_vix()
    macro = _fetch_macro_score()

    # Validate: must be finite positive numbers (baseline is divisor in
    # breakers A/B). NaN comparisons always evaluate False so use `!=` self-check.
    for name, value in (("spy", spy), ("vix", vix)):
        if value is None or value != value or value <= 0:
            raise RuntimeError(
                f"capture_baseline: invalid {name}={value!r} (expected positive number)"
            )
    if macro is None or macro != macro:
        raise RuntimeError(f"capture_baseline: invalid macro={macro!r}")

    return Baseline(
        spy=float(spy),
        vix=float(vix),
        macro_score=float(macro),
        news_cursor_at=dt.datetime.now(dt.timezone.utc),
    )


def _fetch_spy() -> float:
    """Latest SPY trade price via the shared market-data client."""
    from alpaca.data.requests import StockLatestTradeRequest

    def _do():
        resp = _md_client().get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols="SPY")
        )
        return float(resp["SPY"].price)

    return _retry(_do)


def _fetch_vix() -> float:
    """VIX spot via yfinance. Prefers intraday 5-min bars so tick-level
    evaluation (in executor) can see moves within the day; falls back to
    daily bars when intraday is unavailable (e.g., pre-market)."""
    import yfinance as yf

    def _do():
        ticker = yf.Ticker("^VIX")
        intraday = ticker.history(period="1d", interval="5m")
        if not intraday.empty:
            return float(intraday["Close"].iloc[-1])
        daily = ticker.history(period="5d", interval="1d")
        if daily.empty:
            raise RuntimeError("VIX history empty")
        return float(daily["Close"].iloc[-1])

    return _retry(_do)


def _fetch_macro_score() -> float:
    from quant.signals.macro import macro_composite_score
    return float(macro_composite_score())
