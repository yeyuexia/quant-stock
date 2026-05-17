"""Unit tests for data.fetch_ohlcv."""
import sys
import os
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_ohlcv(tickers):
    """Build a minimal MultiIndex OHLCV DataFrame matching yfinance output."""
    dates = pd.date_range("2024-01-01", periods=5, freq="B")
    fields = ["Open", "High", "Low", "Close", "Volume"]
    arrays = [
        [f for f in fields for _ in tickers],
        [t for _ in fields for t in tickers],
    ]
    cols = pd.MultiIndex.from_arrays(arrays, names=["Price", "Ticker"])
    import numpy as np
    data = {(f, t): [100.0] * 5 for f in fields for t in tickers}
    return pd.DataFrame(data, index=dates, columns=cols)


@pytest.fixture(autouse=True)
def no_cache(tmp_path, monkeypatch):
    import data as data_mod
    monkeypatch.setattr(data_mod, "CACHE_DIR", str(tmp_path))


def test_fetch_ohlcv_multi_ticker():
    import data as data_mod
    tickers = ["AAPL", "MSFT"]
    fake_df = _make_ohlcv(tickers)
    with patch("data.yf.download", return_value=fake_df):
        result = data_mod.fetch_ohlcv(tickers, period="1y")
    assert isinstance(result.columns, pd.MultiIndex)
    assert "Close" in result.columns.get_level_values(0)
    assert "AAPL" in result.columns.get_level_values(1)
    assert "MSFT" in result.columns.get_level_values(1)


def test_fetch_ohlcv_single_ticker_wraps_columns():
    import data as data_mod
    tickers = ["AAPL"]
    # yfinance sometimes returns flat columns for single-ticker calls
    fake_df = _make_ohlcv(tickers)
    with patch("data.yf.download", return_value=fake_df):
        result = data_mod.fetch_ohlcv(tickers, period="1y")
    assert isinstance(result.columns, pd.MultiIndex)
    assert "AAPL" in result.columns.get_level_values(1)


def test_fetch_ohlcv_uses_cache(tmp_path):
    import data as data_mod
    tickers = ["AAPL"]
    fake_df = _make_ohlcv(tickers)
    call_count = {"n": 0}

    def fake_download(*a, **kw):
        call_count["n"] += 1
        return fake_df

    with patch("data.yf.download", side_effect=fake_download):
        data_mod.fetch_ohlcv(tickers, period="6mo")
        data_mod.fetch_ohlcv(tickers, period="6mo")  # second call should hit cache

    assert call_count["n"] == 1
