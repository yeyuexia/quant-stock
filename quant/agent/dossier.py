"""Pure per-candidate dossier assembly for the investor agent. No network I/O —
all inputs (info dict, OHLCV frames, news, estimates) are passed in, so every
function here is deterministic and unit-testable."""
from typing import Optional

import pandas as pd

from quant.data.fundamentals import from_info


def _pct_from(price: Optional[float], ref: Optional[float]) -> Optional[float]:
    if price is None or ref is None or ref == 0:
        return None
    return price / ref - 1.0


def _rsi(close: "pd.Series", period: int) -> Optional[float]:
    if close is None or len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _rel_strength(tkr_close, spy_close, lookback: int) -> Optional[float]:
    if tkr_close is None or spy_close is None:
        return None
    if len(tkr_close) <= lookback or len(spy_close) <= lookback:
        return None
    t = tkr_close.iloc[-1] / tkr_close.iloc[-lookback - 1] - 1.0
    s = spy_close.iloc[-1] / spy_close.iloc[-lookback - 1] - 1.0
    return float(t - s)


def _zscore(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return [None] * len(values)
    mean = sum(nums) / len(nums)
    var = sum((v - mean) ** 2 for v in nums) / len(nums)
    sd = var ** 0.5
    if sd == 0:
        return [0.0 if isinstance(v, (int, float)) else None for v in values]
    return [((v - mean) / sd) if isinstance(v, (int, float)) else None for v in values]
