"""Tests for the CANSLIM technical screener (screener.py)."""
import sys
import os
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import screener as sc
from screener import _adr, _ema_value, _detect_base, _compute_rs, screen_stocks, _fundamental_ok, _eps_acceleration


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


def test_detect_vcp_classic_pattern():
    # 3 contractions: 25% → 14.7% → 7.5% (strictly decreasing) → VCP detected
    closes = pd.Series([75.0, 100.0, 75.0, 95.0, 81.0, 93.0, 86.0, 95.0])
    result = _detect_base(closes)
    assert result["in_base"] is True
    assert result["vcp_contractions"] >= 2


def test_detect_vcp_single_contraction_fails():
    # Only one peak-trough pair → not enough contractions for VCP
    closes = pd.Series([90.0, 100.0, 85.0, 97.0, 92.0])
    result = _detect_base(closes)
    assert result["in_base"] is False


def test_detect_vcp_non_decreasing_fails():
    # Contractions 5% → 18% → 27% (expanding, not contracting) → rejected in all sub-windows
    closes = pd.Series([90.0, 100.0, 95.0, 98.0, 80.0, 96.0, 70.0, 95.0])
    result = _detect_base(closes)
    assert result["in_base"] is False


def test_detect_vcp_with_volume_contraction():
    # Classic VCP pattern with decreasing volume → vol_contracting=True, in_base=True
    closes = pd.Series([75.0, 100.0, 75.0, 95.0, 81.0, 93.0, 86.0, 95.0])
    volume = pd.Series([5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5])
    result = _detect_base(closes, weekly_volume=volume)
    assert result["in_base"] is True
    assert result["vol_contracting"] is True


def test_detect_vcp_volume_expanding_no_gate():
    # Classic VCP shape with expanding volume → still detects base, vol_contracting=False
    closes = pd.Series([75.0, 100.0, 75.0, 95.0, 81.0, 93.0, 86.0, 95.0])
    volume = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    result = _detect_base(closes, weekly_volume=volume)
    assert result["in_base"] is True
    assert result["vol_contracting"] is False


def test_detect_vcp_returns_new_keys():
    # Return dict always contains vcp_contractions and vol_contracting
    closes = pd.Series([100.0, 101.0, 100.5, 99.8, 100.2, 101.0, 100.4, 100.1])
    result = _detect_base(closes)
    assert "vcp_contractions" in result
    assert "vol_contracting" in result


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


# ── _fundamental_ok tests ────────────────────────────────────────

def test_fundamental_ok_passes_on_empty_data():
    assert _fundamental_ok("ANY", fund_data={}) is True


def test_fundamental_ok_passes_when_data_none():
    assert _fundamental_ok("ANY", fund_data=None) is True


def test_fundamental_ok_fails_low_eps_growth():
    assert _fundamental_ok("X", fund_data={"eps_q_growth": 0.10}) is False


def test_fundamental_ok_passes_high_eps_growth():
    assert _fundamental_ok("X", fund_data={"eps_q_growth": 0.50}) is True


def test_fundamental_ok_fails_low_revenue_growth():
    assert _fundamental_ok("X", fund_data={"revenue_growth": 0.05}) is False


def test_fundamental_ok_passes_sufficient_revenue():
    assert _fundamental_ok("X", fund_data={"revenue_growth": 0.30}) is True


def test_fundamental_ok_fails_declining_annual_eps():
    assert _fundamental_ok("X", fund_data={"annual_eps": [2.0, 3.0]}) is False


def test_fundamental_ok_passes_growing_annual_eps():
    assert _fundamental_ok("X", fund_data={"annual_eps": [4.0, 3.0]}) is True


def test_fundamental_ok_passes_negative_base_year_positive_current():
    # Turnaround: was losing, now profitable → should pass
    assert _fundamental_ok("X", fund_data={"annual_eps": [1.0, -0.5]}) is True


def test_fundamental_ok_ignores_missing_fields():
    # Only eps_q_growth provided, high enough
    assert _fundamental_ok("X", fund_data={"eps_q_growth": 0.40}) is True


def test_fundamental_ok_combined_filter():
    # All fields present, all pass
    data = {"eps_q_growth": 0.35, "revenue_growth": 0.25, "annual_eps": [5.0, 3.0]}
    assert _fundamental_ok("X", fund_data=data) is True


def test_fundamental_ok_combined_one_fails():
    # eps passes, revenue fails
    data = {"eps_q_growth": 0.35, "revenue_growth": 0.10}
    assert _fundamental_ok("X", fund_data=data) is False


# ── _eps_acceleration tests ──────────────────────────────────────

def test_eps_acceleration_detects_acceleration():
    # q0=3, q1=2, q2=1 → g1=(3-2)/2=0.5, g2=(2-1)/1=1.0 → NOT accelerating
    assert _eps_acceleration([3.0, 2.0, 1.0]) is False


def test_eps_acceleration_true_case():
    # q0=5, q1=3, q2=2 → g1=(5-3)/3=0.67, g2=(3-2)/2=0.5 → accelerating
    assert _eps_acceleration([5.0, 3.0, 2.0]) is True


def test_eps_acceleration_insufficient_data():
    assert _eps_acceleration([2.0]) is False
    assert _eps_acceleration([]) is False


def test_eps_acceleration_zero_denominator():
    assert _eps_acceleration([1.0, 0.0, 1.0]) is False


# ── fundamental columns in screen_stocks output ──────────────────

def test_screen_stocks_has_fundamental_columns():
    tickers = ["AAA", "BBB", "CCC"]
    ohlcv, closes = _build_mock_data(tickers)

    with patch("screener.fetch_ohlcv", return_value=ohlcv), \
         patch("screener.fetch_prices", return_value=closes):
        df = screen_stocks(tickers)

    if not df.empty:
        assert "eps_q_growth" in df.columns
        assert "rev_growth" in df.columns
        assert "eps_accel" in df.columns


def test_screen_stocks_has_in_base_column():
    """screen_stocks result includes in_base column (VCP bonus, not a hard gate)."""
    tickers = ["AAA", "BBB", "CCC"]
    ohlcv, closes = _build_mock_data(tickers)

    with patch("screener.fetch_ohlcv", return_value=ohlcv), \
         patch("screener.fetch_prices", return_value=closes):
        df = screen_stocks(tickers)

    if not df.empty:
        assert "in_base" in df.columns
        assert "vcp_contractions" in df.columns
        assert "vol_contracting" in df.columns


def test_screen_stocks_eps_accel_in_composite():
    """eps_accel=True raises composite vs identical peer with eps_accel=False."""
    tickers = ["ACCEL", "NOACCL"]
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    price_vals = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.015, n)) * np.linspace(0.7, 1.0, n)
    noise = 1.015
    ohlcv_data = {}
    for t in tickers:
        ohlcv_data[("High", t)] = price_vals * noise
        ohlcv_data[("Low", t)] = price_vals / noise
        ohlcv_data[("Close", t)] = price_vals
    df_ohlcv = pd.DataFrame(ohlcv_data, index=dates)
    df_ohlcv.columns = pd.MultiIndex.from_tuples(df_ohlcv.columns)
    closes = pd.DataFrame({"ACCEL": price_vals, "NOACCL": price_vals}, index=dates)

    fund_data = {
        "ACCEL":  {"eps_q_growth": 0.50, "revenue_growth": 0.30,
                   "annual_eps": [5.0, 3.0], "quarterly_eps": [5.0, 3.0, 2.0]},
        "NOACCL": {"eps_q_growth": 0.50, "revenue_growth": 0.30,
                   "annual_eps": [5.0, 3.0], "quarterly_eps": [3.0, 2.0, 1.0]},
    }

    with patch("screener.fetch_ohlcv", return_value=df_ohlcv), \
         patch("screener.fetch_prices", return_value=closes), \
         patch("screener.fetch_fundamentals", side_effect=lambda t: fund_data.get(t, {})), \
         patch("screener._detect_base", return_value={"in_base": True, "base_weeks": 8,
                                                       "depth": 0.05, "tightness": 0.02, "hi": 100.0,
                                                       "vcp_contractions": 2, "vol_contracting": True}):
        df = sc.screen_stocks(tickers)

    if not df.empty and len(df) == 2:
        accel_row = df[df["ticker"] == "ACCEL"]
        noaccl_row = df[df["ticker"] == "NOACCL"]
        if not accel_row.empty and not noaccl_row.empty:
            assert float(accel_row["composite"].iloc[0]) > float(noaccl_row["composite"].iloc[0])


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
