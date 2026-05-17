"""Technical indicators. Pure compute — callers fetch the data.

Add new indicators here when more than one consumer needs them. Keep
implementations free of I/O so they can be unit-tested with synthetic input.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> Optional[float]:
    """Wilder-smoothed Average True Range. Returns the most-recent ATR value.

    Returns None when there is insufficient data (fewer than period+1 aligned
    non-NaN bars).
    """
    df = pd.DataFrame({"high": high, "low": low, "close": close}).dropna()
    if len(df) < period + 1:
        return None

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    # First TR has no prev_close — fall back to high - low for that bar.
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]

    # Initial ATR = simple mean of first `period` TR values.
    initial = tr.iloc[:period].mean()
    atr_val = float(initial)
    # Wilder smoothing for the remaining bars.
    for t in tr.iloc[period:]:
        atr_val = (atr_val * (period - 1) + float(t)) / period
    return atr_val
