"""Unit tests for data.fetch_ohlcv."""
import sys
import os
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
import quant.data.market as data
import json
import logging

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
    import quant.data.market as data_mod
    monkeypatch.setattr(data_mod, "CACHE_DIR", str(tmp_path))


def test_fetch_ohlcv_multi_ticker():
    import quant.data.market as data_mod
    tickers = ["AAPL", "MSFT"]
    fake_df = _make_ohlcv(tickers)
    with patch("quant.data.market.yf.download", return_value=fake_df):
        result = data_mod.fetch_ohlcv(tickers, period="1y")
    assert isinstance(result.columns, pd.MultiIndex)
    assert "Close" in result.columns.get_level_values(0)
    assert "AAPL" in result.columns.get_level_values(1)
    assert "MSFT" in result.columns.get_level_values(1)


def test_fetch_ohlcv_single_ticker_wraps_columns():
    import quant.data.market as data_mod
    tickers = ["AAPL"]
    # yfinance sometimes returns flat columns for single-ticker calls
    fake_df = _make_ohlcv(tickers)
    with patch("quant.data.market.yf.download", return_value=fake_df):
        result = data_mod.fetch_ohlcv(tickers, period="1y")
    assert isinstance(result.columns, pd.MultiIndex)
    assert "AAPL" in result.columns.get_level_values(1)


def test_fetch_ohlcv_uses_cache(tmp_path):
    import quant.data.market as data_mod
    tickers = ["AAPL"]
    fake_df = _make_ohlcv(tickers)
    call_count = {"n": 0}

    def fake_download(*a, **kw):
        call_count["n"] += 1
        return fake_df

    with patch("quant.data.market.yf.download", side_effect=fake_download):
        data_mod.fetch_ohlcv(tickers, period="6mo")
        data_mod.fetch_ohlcv(tickers, period="6mo")  # second call should hit cache

    assert call_count["n"] == 1


# ======================================================================
# Post-review additions (formerly test_data_optimizations.py)
# ======================================================================

"""Regression tests for data.py hardening (D1, D2, D5, D6, D7, D9-D11, D13, D16)."""
import json
import logging
import os
import sys
import pytest
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.data.market as data


# ── D1: string ticker auto-wraps to list, doesn't pollute cache ────

def test_normalize_tickers_string_to_list():
    assert data._normalize_tickers("NVDA") == ["NVDA"]
    assert data._normalize_tickers(["A", "B"]) == ["A", "B"]
    assert data._normalize_tickers([]) == []


def test_fetch_prices_string_input_uses_clean_cache_key(tmp_path, monkeypatch):
    """Old behavior: sorted("NVDA") = ['A','D','N','V'] → cache key
    "prices_A_D_N_V_2y" (garbage). New behavior: ["NVDA"]."""
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))

    # Stub yf.download to return a minimal frame
    fake = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.date_range("2026-05-01", periods=2),
    )
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: fake)

    data.fetch_prices("NVDA", period="2y")
    files = sorted(os.listdir(tmp_path))
    # Should be exactly one prices_NVDA_2y.csv — not garbage like prices_A_D_N_V_2y
    csv_files = [f for f in files if f.startswith("prices_") and f.endswith(".csv")]
    assert len(csv_files) == 1
    assert csv_files[0] == "prices_NVDA_2y.csv"


def test_fetch_prices_empty_list_returns_empty():
    """Empty input → empty DataFrame, no yfinance call."""
    assert data.fetch_prices([]).empty


# ── D5: fetch_ohlcv always returns MultiIndex columns ──────────────

def test_fetch_ohlcv_single_ticker_returns_multiindex(tmp_path, monkeypatch):
    """yfinance returns FLAT cols for single ticker; we wrap into MultiIndex
    so callers don't need their own shape-detection fallback."""
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))

    # FLAT columns (yfinance single-ticker behavior)
    flat = pd.DataFrame({
        "Open": [100, 101],
        "High": [101, 102],
        "Low":  [99, 100],
        "Close": [100, 101],
        "Volume": [1_000_000, 1_100_000],
    }, index=pd.date_range("2026-05-01", periods=2))
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: flat)

    out = data.fetch_ohlcv("NVDA", period="1y")
    assert isinstance(out.columns, pd.MultiIndex)
    # field × ticker
    assert ("Close", "NVDA") in out.columns
    assert ("Volume", "NVDA") in out.columns


def test_fetch_ohlcv_multi_ticker_preserves_multiindex(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))

    idx = pd.date_range("2026-05-01", periods=2)
    multi = pd.DataFrame({
        ("Close", "AAPL"): [100, 101],
        ("Close", "NVDA"): [200, 201],
        ("Volume", "AAPL"): [1e6, 1.1e6],
        ("Volume", "NVDA"): [2e6, 2.1e6],
    }, index=idx)
    multi.columns = pd.MultiIndex.from_tuples(multi.columns)
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: multi)

    out = data.fetch_ohlcv(["AAPL", "NVDA"])
    assert isinstance(out.columns, pd.MultiIndex)
    assert ("Close", "AAPL") in out.columns
    assert ("Close", "NVDA") in out.columns


# ── D7: cache writes go through fcntl atomic helper ────────────────

def test_fetch_prices_creates_lock_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))
    fake = pd.DataFrame(
        {"Close": [100.0]}, index=pd.date_range("2026-05-01", periods=1),
    )
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: fake)

    data.fetch_prices(["SPY"], period="5d")
    # The fileio atomic helper writes a .lock sidecar
    assert (tmp_path / "prices_SPY_5d.csv.lock").exists()
    assert (tmp_path / "prices_SPY_5d.csv").exists()


def test_fetch_ohlcv_creates_lock_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))
    flat = pd.DataFrame({
        "Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [1e6],
    }, index=pd.date_range("2026-05-01", periods=1))
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: flat)

    data.fetch_ohlcv(["SPY"], period="5d")
    parquet_files = list(tmp_path.glob("*.parquet"))
    assert len(parquet_files) == 1
    assert (str(parquet_files[0]) + ".lock") in [str(p) for p in tmp_path.iterdir()]


# ── D6: fetch_info fail-open on yfinance failure ───────────────────

def test_fetch_info_returns_empty_on_failure(tmp_path, monkeypatch, caplog):
    """Old behavior: raised — caller had to wrap. New: returns {} like
    fetch_fundamentals does, with a warning logged."""
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))

    class FailingTicker:
        def __init__(self, sym): pass
        @property
        def info(self):
            raise RuntimeError("yfinance dead")
    monkeypatch.setattr("yfinance.Ticker", FailingTicker)

    with caplog.at_level(logging.WARNING, logger="data"):
        result = data.fetch_info("ZZZZ")
    assert result == {}
    assert any("yfinance failed" in r.message for r in caplog.records)


def test_fetch_info_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))
    # Pre-seed a cache file
    cache_path = tmp_path / "info_NVDA.json"
    cache_path.write_text(json.dumps({"sector": "Tech", "marketCap": 1e12}))

    # Stub yfinance to fail loud so we know we read from cache
    class BoomTicker:
        def __init__(self, sym): pass
        @property
        def info(self):
            raise AssertionError("should not have hit yfinance")
    monkeypatch.setattr("yfinance.Ticker", BoomTicker)

    result = data.fetch_info("NVDA")
    assert result == {"sector": "Tech", "marketCap": 1e12}


# ── D11: fetch_fundamentals logs warnings on partial failure ───────

def test_fetch_fundamentals_logs_when_quarterly_stmt_fails(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))

    class PartialTicker:
        def __init__(self, sym): pass
        @property
        def info(self):
            return {"earningsQuarterlyGrowth": 0.25, "revenueGrowth": 0.20}
        @property
        def quarterly_income_stmt(self):
            raise RuntimeError("statement endpoint down")
        @property
        def income_stmt(self):
            return None
    monkeypatch.setattr("yfinance.Ticker", PartialTicker)

    with caplog.at_level(logging.WARNING, logger="data"):
        result = data.fetch_fundamentals("ZZZZ")

    # info-derived fields populated
    assert result.get("eps_q_growth") == 0.25
    # Statement-derived fields absent
    assert "quarterly_eps" not in result
    # Warning logged for the failed branch
    assert any("quarterly_income_stmt failed" in r.message for r in caplog.records)


# ── D10: fetch_fundamentals reuses fetch_info's cache ──────────────

def test_fetch_fundamentals_reuses_fetch_info_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", str(tmp_path))
    # Seed info cache directly
    (tmp_path / "info_AAPL.json").write_text(json.dumps({
        "earningsQuarterlyGrowth": 0.30,
        "revenueGrowth": 0.18,
    }))

    info_hits = {"n": 0}
    stmt_hits = {"n": 0}

    class CountingTicker:
        def __init__(self, sym): pass
        @property
        def info(self):
            info_hits["n"] += 1
            raise AssertionError("should not have hit info — cache should serve")
        @property
        def quarterly_income_stmt(self):
            stmt_hits["n"] += 1
            return None
        @property
        def income_stmt(self):
            return None
    monkeypatch.setattr("yfinance.Ticker", CountingTicker)

    result = data.fetch_fundamentals("AAPL")
    # info served from cache via fetch_info reuse
    assert info_hits["n"] == 0
    assert result.get("eps_q_growth") == 0.30
    assert result.get("revenue_growth") == 0.18


# ── D2: retry helper succeeds on second attempt ────────────────────

def test_retry_succeeds_on_second_attempt():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"
    assert data._retry(flaky) == "ok"
    assert calls["n"] == 2


def test_retry_raises_after_all_attempts():
    def always_fail():
        raise RuntimeError("dead")
    with pytest.raises(RuntimeError, match="dead"):
        data._retry(always_fail)


# ── D9: FUNDAMENTALS_TTL_HOURS is a named constant ─────────────────

def test_fundamentals_ttl_is_constant():
    assert hasattr(data, "FUNDAMENTALS_TTL_HOURS")
    assert data.FUNDAMENTALS_TTL_HOURS == 24


# ── D13: shared executor exists ────────────────────────────────────

def test_shared_executor_exists():
    """_EXECUTOR module-level ThreadPoolExecutor avoids per-call fork/join."""
    assert hasattr(data, "_EXECUTOR")
    from concurrent.futures import ThreadPoolExecutor
    assert isinstance(data._EXECUTOR, ThreadPoolExecutor)


# ── D16: cache key consistency ─────────────────────────────────────

def test_cache_key_short_uses_raw_form():
    key = data._cache_key("prices", ["SPY", "QQQ"], "2y")
    assert key == "prices_QQQ_SPY_2y"   # sorted, raw


def test_cache_key_long_falls_back_to_md5():
    # Construct a tickers list long enough to exceed 200 chars in the raw key
    long_tickers = [f"T{i:04d}" for i in range(50)]
    key = data._cache_key("prices", long_tickers, "2y")
    assert key.startswith("prices_")
    # Should be hex hash, not all the ticker names joined
    assert len(key.split("_")[1]) == 32  # md5 hex
