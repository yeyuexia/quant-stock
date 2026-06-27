"""Unit tests for indicators.atr — Wilder-smoothed ATR(14)."""
import math
import numpy as np
import pandas as pd
import pytest

from quant.signals.indicators import atr
import os
import sys


def _series(values):
    """Build a daily-indexed Series for the given values."""
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_atr_constant_true_range_returns_that_range():
    """20 bars where every TR = 1.0 → ATR converges to 1.0."""
    n = 20
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    # TR per bar: max(11-10=1, |11-10.5|=0.5, |10-10.5|=0.5) = 1.0 for every bar
    # except the very first which has no prev_close (uses high-low=1.0 anyway).
    result = atr(high, low, close, period=14)
    assert result is not None
    assert math.isclose(result, 1.0, rel_tol=1e-9)


def test_atr_insufficient_data_returns_none():
    """Fewer than period+1 bars → None."""
    high  = _series([11.0] * 10)
    low   = _series([10.0] * 10)
    close = _series([10.5] * 10)
    assert atr(high, low, close, period=14) is None


def test_atr_constant_price_returns_zero():
    """high == low == close throughout → TR=0 → ATR=0."""
    n = 20
    high = low = close = _series([100.0] * n)
    result = atr(high, low, close, period=14)
    assert result == 0.0


def test_atr_handles_nan_inputs():
    """NaN values in inputs do not crash; ATR is computed from the non-NaN tail."""
    n = 25
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    high.iloc[0]  = float("nan")
    low.iloc[0]   = float("nan")
    close.iloc[0] = float("nan")
    result = atr(high, low, close, period=14)
    # 24 valid bars remain; ATR is well-defined and equals 1.0 by the same
    # constant-TR argument as the first test.
    assert result is not None
    assert math.isclose(result, 1.0, rel_tol=1e-9)


def test_atr_wilder_step_matches_recurrence():
    """One-step Wilder update: ATR[t] = (ATR[t-1]*(n-1) + TR[t]) / n.

    Build 15 bars with constant TR=1, then a 16th bar with TR=2.
    Expected: ATR[14] = 1.0, ATR[15] = (1.0*13 + 2.0)/14 = 15/14.
    """
    n = 16
    # Bars 0..14: high-low = 1.0, close mid (TR=1.0)
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    # Bar 15: widen so TR = 2.0 (high=12, low=10, prev_close=10.5 → high-low=2)
    high.iloc[-1]  = 12.0
    low.iloc[-1]   = 10.0
    close.iloc[-1] = 11.0
    result = atr(high, low, close, period=14)
    expected = (1.0 * 13 + 2.0) / 14
    assert result is not None
    assert math.isclose(result, expected, rel_tol=1e-9)


# ======================================================================
# Post-review additions (formerly test_indicators_optimizations.py)
# ======================================================================

"""Regression tests for indicators.py defensive validation."""
import os
import sys
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from quant.signals.indicators import atr


def test_atr_returns_none_on_invalid_period():
    s = pd.Series([100, 101, 102, 103, 104])
    assert atr(s, s, s, period=0) is None
    assert atr(s, s, s, period=-1) is None


def test_atr_returns_none_on_short_history():
    s = pd.Series([100, 101, 102])
    assert atr(s, s, s, period=14) is None


def test_atr_returns_none_on_bad_high_low():
    """If high < low (data error), refuse to compute — wrong ATR would
    propagate into wrong ATR-scaled stop placement."""
    high = pd.Series([100, 101, 90, 103])    # bar 3: high < low
    low  = pd.Series([99,  100, 95, 102])
    close = pd.Series([100, 101, 92, 103])
    # Pad to ensure length passes
    high = pd.concat([high, pd.Series([105] * 20)], ignore_index=True)
    low  = pd.concat([low,  pd.Series([104] * 20)], ignore_index=True)
    close = pd.concat([close, pd.Series([104.5] * 20)], ignore_index=True)
    assert atr(high, low, close, period=14) is None


def test_atr_basic_synthetic():
    """Constant 1-point range → ATR should converge to 1."""
    n = 100
    high = pd.Series([101.0] * n)
    low = pd.Series([100.0] * n)
    close = pd.Series([100.5] * n)
    val = atr(high, low, close, period=14)
    assert val is not None
    assert abs(val - 1.0) < 0.01
