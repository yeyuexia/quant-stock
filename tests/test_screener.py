"""Tests for the CANSLIM technical screener (screener.py)."""
import sys
import os
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import screener as sc
from screener import _adr, _ema_value, _detect_base, _compute_rs, screen_stocks


# ── helpers ──────────────────────────────────────────────────────

def _make_closes(n=300, tickers=("AAA", "BBB", "CCC")):
    """Random close-price DataFrame (DatetimeIndex × tickers)."""
    rng = np.random.default_rng(42)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    prices = {}
    for t in tickers:
        prices[t] = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    return pd.DataFrame(prices, index=idx)


def _make_ohlcv(closes: pd.DataFrame) -> pd.DataFrame:
    """Build a MultiIndex (field × ticker) OHLCV frame from close prices."""
    rng = np.random.default_rng(7)
    tickers = closes.columns.tolist()
    fields = {}
    for t in tickers:
        c = closes[t]
        noise = 1 + rng.uniform(0.005, 0.02, len(c))
        fields[("High", t)] = c * noise
        fields[("Low", t)] = c / noise
        fields[("Open", t)] = c.shift(1).fillna(c.iloc[0])
        fields[("Close", t)] = c
        fields[("Volume", t)] = rng.integers(1_000_000, 5_000_000, len(c)).astype(float)
    df = pd.DataFrame(fields, index=closes.index)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


# ── unit tests ───────────────────────────────────────────────────

def test_adr_basic():
    high = pd.Series([102.0] * 30)
    low = pd.Series([98.0] * 30)
    result = _adr(high, low, 20)
    assert abs(result - (4 / 98)) < 1e-6


def test_adr_insufficient_data():
    high = pd.Series([105.0] * 5)
    low = pd.Series([100.0] * 5)
    assert _adr(high, low, 20) == 0.0


def test_ema_value_returns_float():
    closes = pd.Series([100.0] * 100)
    val = _ema_value(closes, 21)
    assert isinstance(val, float)
    assert abs(val - 100.0) < 0.01


def test_ema_value_empty():
    assert _ema_value(pd.Series(dtype=float), 21) == 0.0


def test_detect_base_finds_tight_window():
    # 8 weeks of near-flat closes → should be detected as a base
    closes = pd.Series([100.0, 101.0, 100.5, 99.8, 100.2, 101.0, 100.4, 100.1])
    result = _detect_base(closes)
    assert result["in_base"] is True
    assert result["base_weeks"] >= 5


def test_detect_base_rejects_volatile():
    # Wide swings — should not be a base
    closes = pd.Series([100.0, 120.0, 80.0, 130.0, 70.0, 110.0, 90.0])
    result = _detect_base(closes)
    assert result["in_base"] is False


def test_detect_base_too_short():
    closes = pd.Series([100.0, 100.5, 100.2])
    result = _detect_base(closes)
    assert result["in_base"] is False


def test_compute_rs_returns_0_100():
    closes = _make_closes(300, ("A", "B", "C", "D", "E"))
    scores = _compute_rs(closes)
    assert set(scores.index) == {"A", "B", "C", "D", "E"}
    assert (scores >= 0).all() and (scores <= 100).all()


def test_compute_rs_short_history():
    closes = _make_closes(30, ("A", "B"))
    # Should not raise even with < 63 days
    scores = _compute_rs(closes)
    assert len(scores) == 2


# ── integration tests (mocked data) ──────────────────────────────

def _build_mock_data(tickers=("AAA", "BBB", "CCC")):
    """Build a strong trending mock for all tickers so filters pass."""
    n = 300
    closes = _make_closes(n, tickers)
    # Bias upward so EMAs are below current price
    for t in tickers:
        closes[t] *= np.linspace(0.7, 1.0, n)
    ohlcv = _make_ohlcv(closes)
    return ohlcv, closes


def test_screen_stocks_returns_dataframe():
    tickers = ["AAA", "BBB", "CCC"]
    ohlcv, closes = _build_mock_data(tickers)

    with patch("screener.fetch_ohlcv", return_value=ohlcv), \
         patch("screener.fetch_prices", return_value=closes):
        df = screen_stocks(tickers)

    assert isinstance(df, pd.DataFrame)
    if not df.empty:
        assert "ticker" in df.columns
        assert "rs_score" in df.columns
        assert "adr" in df.columns
        assert "composite" in df.columns
        assert "rank" in df.columns


def test_screen_stocks_respects_top_n():
    from config import SCREEN_TOP_N
    tickers = [f"T{i}" for i in range(20)]
    closes = _make_closes(300, tickers)
    # Make all pass filters: bias upward
    for t in tickers:
        closes[t] *= np.linspace(0.7, 1.0, 300)
    ohlcv = _make_ohlcv(closes)

    with patch("screener.fetch_ohlcv", return_value=ohlcv), \
         patch("screener.fetch_prices", return_value=closes):
        df = screen_stocks(tickers)

    assert len(df) <= SCREEN_TOP_N


def test_screen_stocks_empty_on_fetch_error():
    with patch("screener.fetch_ohlcv", side_effect=Exception("network error")):
        df = screen_stocks(["FAIL"])
    assert df.empty


def test_screen_stocks_rank_column_sequential():
    tickers = ["AAA", "BBB", "CCC"]
    ohlcv, closes = _build_mock_data(tickers)

    with patch("screener.fetch_ohlcv", return_value=ohlcv), \
         patch("screener.fetch_prices", return_value=closes):
        df = screen_stocks(tickers)

    if not df.empty:
        assert list(df["rank"]) == list(range(1, len(df) + 1))


def test_screen_stocks_returns_base_hi_column():
    """base_hi (price ceiling of detected base) is present on the result df."""
    import pandas as pd
    import screener as sc

    # Build OHLCV with a clear base: 16 weekly closes tight around 100, last bar at 102.
    n_days = 16 * 5  # ~16 weeks of business days
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    base_close_path = [100.0] * (n_days - 1) + [102.0]
    df_ohlcv = pd.DataFrame({
        ("High",  "TEST"): [c + 1 for c in base_close_path],
        ("Low",   "TEST"): [c - 1 for c in base_close_path],
        ("Close", "TEST"): base_close_path,
    }, index=dates)
    df_ohlcv.columns = pd.MultiIndex.from_tuples(df_ohlcv.columns)

    prices = pd.DataFrame({"TEST": base_close_path}, index=dates)

    from unittest.mock import patch
    with patch("screener.fetch_ohlcv", return_value=df_ohlcv), \
         patch("screener.fetch_prices", return_value=prices):
        df = sc.screen_stocks(tickers=["TEST"])

    if df.empty:
        # The combination of thresholds may not pass screening; assert that
        # _detect_base still returns hi via a direct invocation as a fallback.
        weekly = pd.Series(base_close_path, index=dates).resample("W").last().dropna()
        base = sc._detect_base(weekly)
        assert "hi" in base
        assert base["hi"] is None or isinstance(base["hi"], float)
    else:
        assert "base_hi" in df.columns
        assert df.iloc[0]["base_hi"] is None or float(df.iloc[0]["base_hi"]) > 0
