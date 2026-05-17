"""Unit tests for indicators.atr — Wilder-smoothed ATR(14)."""
import math
import numpy as np
import pandas as pd
import pytest

from indicators import atr


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
