# baseline.py
"""Captures plan-time market snapshots that circuit breakers diff against.

All fetchers are private functions to make the public surface easy to mock
in tests: just patch _fetch_spy / _fetch_vix / _fetch_macro_score.
"""
from __future__ import annotations
import datetime as dt

from pending_plan import Baseline


def capture_baseline() -> Baseline:
    return Baseline(
        spy=_fetch_spy(),
        vix=_fetch_vix(),
        macro_score=_fetch_macro_score(),
        news_cursor_at=dt.datetime.now(dt.timezone.utc),
    )


def _fetch_spy() -> float:
    import os
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    md = StockHistoricalDataClient(api_key=key, secret_key=secret)
    resp = md.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols="SPY"))
    return float(resp["SPY"].price)


def _fetch_vix() -> float:
    import yfinance as yf
    hist = yf.Ticker("^VIX").history(period="5d", interval="1d")
    if hist.empty:
        raise RuntimeError("VIX history empty")
    return float(hist["Close"].iloc[-1])


def _fetch_macro_score() -> float:
    from macro import macro_composite_score
    return float(macro_composite_score())
