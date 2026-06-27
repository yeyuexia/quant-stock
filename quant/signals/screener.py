"""
Strategy 2: CANSLIM Technical Stock Screener

Screens the watchlist for stocks in technical leadership:
  - C+A fundamental pre-filter: quarterly EPS YoY ≥25%, revenue YoY ≥20%,
    annual EPS growing (applied before fetching price data)
  - Relative Strength (RS) percentile vs the screening universe ≥ SCREEN_RS_MIN
  - Average Daily Range ≥ SCREEN_ADR_MIN  (liquidity / tradability)
  - Price above EMA_FAST and EMA_SLOW     (trend filter)
  - Base pattern bonus: tight consolidation (medium: depth + tightness)

Replaces the old value/quality (Magic Formula) screener.
"""
from typing import Optional, List
import math
import numpy as np
import pandas as pd
from quant.data.market import fetch_ohlcv, fetch_prices, fetch_fundamentals
from quant.config import (
    WATCHLIST, SCREEN_TOP_N,
    SCREEN_RS_MIN, SCREEN_ADR_MIN, SCREEN_ADR_PERIOD,
    SCREEN_EMA_FAST, SCREEN_EMA_SLOW,
    SCREEN_BASE_WEEKS_MIN, SCREEN_BASE_WEEKS_MAX,
    SCREEN_BASE_DEPTH_MAX,
    SCREEN_EPS_Q_GROWTH_MIN, SCREEN_REV_GROWTH_MIN,
)

# Optional hook so discovery.find_stale_watchlist has data to work with.
# Resolved at import time so tests can monkeypatch `screener.record_screener_pass`.
try:
    from quant.data.universe import record_screener_pass
except Exception:
    def record_screener_pass(_tickers):  # noqa: E301
        return None


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


def _find_vcp_contractions(prices: pd.Series) -> list:
    """Return list of (retracement_fraction, peak_price) tuples in chronological order.

    A contraction is measured from a local maximum to the next local minimum.
    Uses strict inequality so flat stretches are not tagged as extrema.
    """
    arr = prices.values
    n = len(arr)
    if n < 4:
        return []

    peaks = []
    troughs = []
    for i in range(1, n - 1):
        if arr[i] > arr[i - 1] and arr[i] > arr[i + 1]:
            peaks.append((i, float(arr[i])))
        elif arr[i] < arr[i - 1] and arr[i] < arr[i + 1]:
            troughs.append((i, float(arr[i])))

    contractions = []
    ti = 0
    for pk_i, pk_v in peaks:
        while ti < len(troughs) and troughs[ti][0] <= pk_i:
            ti += 1
        if ti >= len(troughs):
            break
        tr_v = troughs[ti][1]
        if pk_v > 0:
            contractions.append(((pk_v - tr_v) / pk_v, pk_v))
        ti += 1
    return contractions


def _detect_base(weekly_closes: pd.Series,
                 weekly_volume: Optional[pd.Series] = None) -> dict:
    """VCP (Volatility Contraction Pattern) detection.

    Requires ≥2 peak-to-trough contractions with strictly decreasing retracement
    fractions and overall depth ≤ SCREEN_BASE_DEPTH_MAX. When weekly_volume is
    supplied, also requires average volume in the second half of the base to be
    lower than the first half (volume contraction).
    """
    _empty = {"in_base": False, "base_weeks": 0, "depth": None,
               "tightness": None, "hi": None, "vcp_pivot": None, "vcp_contractions": 0,
               "vol_contracting": False}

    n = len(weekly_closes)
    if n < SCREEN_BASE_WEEKS_MIN:
        return _empty

    for w in range(min(n, SCREEN_BASE_WEEKS_MAX), SCREEN_BASE_WEEKS_MIN - 1, -1):
        window = weekly_closes.iloc[-w:]
        hi = float(window.max())
        lo = float(window.min())
        if hi == 0:
            continue
        depth = (hi - lo) / hi
        if depth > SCREEN_BASE_DEPTH_MAX:
            continue

        contractions = _find_vcp_contractions(window)
        if len(contractions) < 2:
            continue
        retracements = [c[0] for c in contractions]
        if not all(retracements[i] > retracements[i + 1]
                   for i in range(len(retracements) - 1)):
            continue

        vcp_pivot = contractions[-1][1]  # last local peak = right-side handle high = buy point

        vol_contracting = False
        if weekly_volume is not None and len(weekly_volume) >= w:
            vol_w = weekly_volume.iloc[-w:]
            mid = len(vol_w) // 2
            if mid > 0:
                vol_contracting = float(vol_w.iloc[mid:].mean()) < float(vol_w.iloc[:mid].mean())

        tightness = window.std() / window.mean() if window.mean() > 0 else 1.0
        return {
            "in_base": True,
            "base_weeks": w,
            "depth": float(depth),
            "tightness": float(tightness),
            "hi": hi,
            "vcp_pivot": float(vcp_pivot),
            "vcp_contractions": len(contractions),
            "vol_contracting": vol_contracting,
        }

    return {**_empty}


def _eps_acceleration(quarterly_eps: list) -> bool:
    """True if most recent quarter's sequential growth rate exceeds prior quarter's.

    Uses quarter-over-quarter change as proxy when only 3+ quarters are available.
    Both consecutive growth rates must have the same sign (both positive growth).
    """
    if len(quarterly_eps) < 3:
        return False
    q0, q1, q2 = quarterly_eps[0], quarterly_eps[1], quarterly_eps[2]
    if q1 == 0 or q2 == 0:
        return False
    g1 = (q0 - q1) / abs(q1)
    g2 = (q1 - q2) / abs(q2)
    return g1 > g2 and g1 > 0


def _fundamental_ok(ticker: str, fund_data: Optional[dict] = None) -> bool:
    """CANSLIM C+A filter. Fail-open: returns True when data is unavailable.

    Hard filters:
      C1: quarterly EPS YoY >= SCREEN_EPS_Q_GROWTH_MIN (default 25%)
      C2: revenue YoY >= SCREEN_REV_GROWTH_MIN (default 20%)
      A:  annual EPS growing (most recent year > prior year, both positive)
    """
    if fund_data is None:
        try:
            fund_data = fetch_fundamentals(ticker)
        except Exception:
            return True

    if not fund_data:
        return True

    # C1: quarterly EPS YoY >= 25%
    eps_q = fund_data.get("eps_q_growth")
    if eps_q is not None and math.isfinite(eps_q) and eps_q < SCREEN_EPS_Q_GROWTH_MIN:
        return False

    # C2: revenue YoY >= 20%
    rev_g = fund_data.get("revenue_growth")
    if rev_g is not None and math.isfinite(rev_g) and rev_g < SCREEN_REV_GROWTH_MIN:
        return False

    # A: annual EPS growing
    annual = fund_data.get("annual_eps", [])
    if len(annual) >= 2 and annual[1] is not None and annual[1] > 0:
        if annual[0] is not None and annual[0] <= annual[1]:
            return False

    return True


def screen_stocks(
    tickers: Optional[List[str]] = None,
    with_review: bool = False,
) -> "pd.DataFrame | tuple[pd.DataFrame, str | None]":
    """Screen tickers for CANSLIM technical setups.

    Returns ranked DataFrame, or (DataFrame, review_text) when with_review=True.
    review_text is None if investor agent is unavailable or times out.
    """
    _empty = lambda: (pd.DataFrame(), None) if with_review else pd.DataFrame()

    if tickers is None:
        tickers = WATCHLIST

    # ── C+A fundamental pre-filter ─────────────────────────────────
    fund_cache: dict = {}
    passed = []
    for t in tickers:
        try:
            fdata = fetch_fundamentals(t)
        except Exception:
            fdata = {}
        fund_cache[t] = fdata
        if _fundamental_ok(t, fund_data=fdata):
            passed.append(t)
    tickers = passed
    if not tickers:
        return _empty()

    try:
        ohlcv = fetch_ohlcv(tickers, period="1y")
    except Exception:
        return _empty()

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
            try:
                vol = ohlcv["Volume"][t].dropna()
                weekly_vol = vol.resample("W").sum().dropna() if not vol.empty else None
            except (KeyError, TypeError):
                weekly_vol = None
            base = _detect_base(weekly, weekly_volume=weekly_vol)
            rs = float(rs_scores.get(t, 0.0)) if not rs_scores.empty else 0.0

            fdata = fund_cache.get(t, {})
            q_eps_list = fdata.get("quarterly_eps", [])

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
                "base_hi": base.get("hi"),
                "vcp_pivot": base.get("vcp_pivot"),
                "vcp_contractions": base["vcp_contractions"],
                "vol_contracting": base["vol_contracting"],
                "eps_q_growth": fdata.get("eps_q_growth"),
                "rev_growth": fdata.get("revenue_growth"),
                "eps_accel": _eps_acceleration(q_eps_list),
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return _empty()

    df = df[df["adr"] >= SCREEN_ADR_MIN]
    df = df[df["above_ema_fast"] & df["above_ema_slow"]]
    df = df[df["rs_score"] >= SCREEN_RS_MIN]

    if df.empty:
        return _empty()

    # Stamp every survivor of the three technical hard gates so
    # discovery.find_stale_watchlist can age out perpetual underperformers.
    try:
        record_screener_pass(df["ticker"].tolist())
    except Exception:
        pass

    def _norm(series, ascending=True):
        ranked = series.rank(ascending=ascending, na_option="bottom")
        span = ranked.max() - ranked.min() + 1e-9
        return (ranked - ranked.min()) / span

    df["adr_rank"] = _norm(df["adr"], ascending=False)
    df["accel_score"] = df["eps_accel"].fillna(False).astype(float)
    df["vcp_score"] = df["in_base"].fillna(False).astype(float)

    df["composite"] = (
        0.40 * df["adr_rank"]
        + 0.40 * df["accel_score"]
        + 0.20 * df["vcp_score"]
    )

    df = df.sort_values("composite", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    result = df.head(SCREEN_TOP_N)

    if not with_review:
        return result

    from quant.agent.investor import run_investor_review
    review = run_investor_review(result)
    return result, review
