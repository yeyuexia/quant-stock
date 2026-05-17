"""
Strategy 2: CANSLIM Technical Stock Screener

Screens the watchlist for stocks in technical leadership:
  - Relative Strength (RS) percentile vs the screening universe ≥ SCREEN_RS_MIN
  - Average Daily Range ≥ SCREEN_ADR_MIN  (liquidity / tradability)
  - Price above EMA_FAST and EMA_SLOW     (trend filter)
  - Base pattern bonus: tight consolidation (medium: depth + tightness)

Replaces the old value/quality (Magic Formula) screener.
"""
from typing import Optional, List
import numpy as np
import pandas as pd
from data import fetch_ohlcv, fetch_prices
from config import (
    WATCHLIST, SCREEN_TOP_N,
    SCREEN_RS_MIN, SCREEN_ADR_MIN, SCREEN_ADR_PERIOD,
    SCREEN_EMA_FAST, SCREEN_EMA_SLOW,
    SCREEN_BASE_WEEKS_MIN, SCREEN_BASE_WEEKS_MAX,
    SCREEN_BASE_DEPTH_MAX, SCREEN_TIGHTNESS_PCT_MAX,
)


def _compute_rs(closes: pd.DataFrame) -> pd.Series:
    """Composite RS score (0–100 percentile) using 3M/6M/12M return blend."""
    def _ret(n):
        if len(closes) >= n:
            return closes.iloc[-1] / closes.iloc[-n] - 1
        return pd.Series(np.nan, index=closes.columns)

    composite = 0.40 * _ret(63).fillna(0) + 0.30 * _ret(126).fillna(0) + 0.30 * _ret(252).fillna(0)
    return composite.rank(pct=True) * 100


def _adr(high: pd.Series, low: pd.Series, period: int) -> float:
    """Average Daily Range as fraction of low price over the last `period` bars."""
    if len(high) < period:
        return 0.0
    h = high.iloc[-period:]
    l = low.iloc[-period:]
    return float(((h - l) / l.replace(0, np.nan)).mean())


def _ema_value(closes: pd.Series, period: int) -> float:
    """Most recent EMA value."""
    if closes.empty:
        return 0.0
    return float(closes.ewm(span=period, adjust=False).mean().iloc[-1])


def _detect_base(weekly_closes: pd.Series) -> dict:
    """Medium base detection: scan from widest to narrowest valid window.

    A window qualifies when:
      depth     = (hi − lo) / hi        ≤ SCREEN_BASE_DEPTH_MAX
      tightness = std(closes) / mean     ≤ SCREEN_TIGHTNESS_PCT_MAX
      width     between SCREEN_BASE_WEEKS_MIN and SCREEN_BASE_WEEKS_MAX
    """
    n = len(weekly_closes)
    if n < SCREEN_BASE_WEEKS_MIN:
        return {"in_base": False, "base_weeks": 0, "depth": None, "tightness": None}

    for w in range(min(n, SCREEN_BASE_WEEKS_MAX), SCREEN_BASE_WEEKS_MIN - 1, -1):
        window = weekly_closes.iloc[-w:]
        hi = window.max()
        lo = window.min()
        if hi == 0:
            continue
        depth = (hi - lo) / hi
        tightness = window.std() / window.mean() if window.mean() > 0 else 1.0
        if depth <= SCREEN_BASE_DEPTH_MAX and tightness <= SCREEN_TIGHTNESS_PCT_MAX:
            return {
                "in_base": True,
                "base_weeks": w,
                "depth": float(depth),
                "tightness": float(tightness),
            }

    return {"in_base": False, "base_weeks": 0, "depth": None, "tightness": None}


def screen_stocks(tickers: Optional[List[str]] = None) -> pd.DataFrame:
    """Screen tickers for CANSLIM technical setups. Returns ranked DataFrame."""
    if tickers is None:
        tickers = WATCHLIST

    try:
        ohlcv = fetch_ohlcv(tickers, period="1y")
    except Exception:
        return pd.DataFrame()

    try:
        closes = fetch_prices(tickers, period="2y")
        rs_scores = _compute_rs(closes) if not closes.empty else pd.Series(dtype=float)
    except Exception:
        rs_scores = pd.Series(dtype=float)

    rows = []
    for t in tickers:
        try:
            close_all = ohlcv["Close"]
            high_all = ohlcv["High"]
            low_all = ohlcv["Low"]

            close = close_all[t].dropna() if t in close_all.columns else pd.Series(dtype=float)
            high = high_all[t].dropna() if t in high_all.columns else pd.Series(dtype=float)
            low = low_all[t].dropna() if t in low_all.columns else pd.Series(dtype=float)

            if close.empty:
                continue

            price = float(close.iloc[-1])
            adr = _adr(high, low, SCREEN_ADR_PERIOD)
            ema_fast = _ema_value(close, SCREEN_EMA_FAST)
            ema_slow = _ema_value(close, SCREEN_EMA_SLOW)
            above_ema_fast = price > ema_fast
            above_ema_slow = price > ema_slow
            weekly = close.resample("W").last().dropna()
            base = _detect_base(weekly)
            rs = float(rs_scores.get(t, 0.0)) if not rs_scores.empty else 0.0

            rows.append({
                "ticker": t,
                "price": price,
                "rs_score": rs,
                "adr": adr,
                "above_ema_fast": above_ema_fast,
                "above_ema_slow": above_ema_slow,
                "in_base": base["in_base"],
                "base_weeks": base["base_weeks"],
                "base_depth": base["depth"],
                "base_tightness": base["tightness"],
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df[df["adr"] >= SCREEN_ADR_MIN]
    df = df[df["above_ema_fast"] & df["above_ema_slow"]]
    df = df[df["rs_score"] >= SCREEN_RS_MIN]

    if df.empty:
        return df

    def _norm(series, ascending=True):
        ranked = series.rank(ascending=ascending, na_option="bottom")
        span = ranked.max() - ranked.min() + 1e-9
        return (ranked - ranked.min()) / span

    df["rs_rank"] = _norm(df["rs_score"], ascending=False)
    df["adr_rank"] = _norm(df["adr"], ascending=False)
    df["base_bonus"] = df["in_base"].astype(float)

    df["composite"] = (
        0.60 * df["rs_rank"]
        + 0.20 * df["adr_rank"]
        + 0.20 * df["base_bonus"]
    )

    df = df.sort_values("composite", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df.head(SCREEN_TOP_N)
