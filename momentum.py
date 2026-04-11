"""
Strategy 1: Dual Momentum ETF Rotation

Concept (Antonacci, 2014):
  1. Rank ETFs by composite momentum score (weighted blend of 1/3/6/12-month returns).
  2. Apply absolute momentum filter: only hold ETFs above their 200-day SMA.
  3. If fewer than N pass, allocate remainder to safe haven (T-bills).

This is a well-documented, robust strategy that works especially well with small
capital because you only trade a handful of low-cost ETFs once a month.
"""
import pandas as pd
import numpy as np
from data import fetch_prices
from config import (
    ETF_UNIVERSE, SAFE_HAVEN, MOMENTUM_LOOKBACK_MONTHS,
    MOMENTUM_TOP_N, SMA_FILTER_PERIOD,
)


def _momentum_score(prices: pd.Series, months: list[int]) -> float:
    """Composite momentum: weighted average of N-month returns.
    More recent periods get higher weight."""
    scores = []
    weights = []
    for i, m in enumerate(sorted(months)):
        days = m * 21  # approx trading days
        if len(prices) < days + 1:
            continue
        ret = prices.iloc[-1] / prices.iloc[-days] - 1
        w = 1.0 / m  # inverse-month weighting (recent > distant)
        scores.append(ret * w)
        weights.append(w)
    if not weights:
        return -999
    return sum(scores) / sum(weights)


def rank_etfs() -> pd.DataFrame:
    """Return a DataFrame of ETFs ranked by composite momentum with SMA filter."""
    tickers = ETF_UNIVERSE + [SAFE_HAVEN]
    prices = fetch_prices(tickers, period="2y")

    rows = []
    for t in ETF_UNIVERSE:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < SMA_FILTER_PERIOD:
            continue

        score = _momentum_score(s, MOMENTUM_LOOKBACK_MONTHS)
        sma = s.rolling(SMA_FILTER_PERIOD).mean().iloc[-1]
        above_sma = s.iloc[-1] > sma
        current_price = s.iloc[-1]

        # Individual period returns for display
        rets = {}
        for m in MOMENTUM_LOOKBACK_MONTHS:
            days = m * 21
            if len(s) >= days + 1:
                rets[f"{m}m_ret"] = s.iloc[-1] / s.iloc[-days] - 1
            else:
                rets[f"{m}m_ret"] = None

        rows.append({
            "ticker": t,
            "price": current_price,
            "momentum_score": score,
            "above_sma200": above_sma,
            **rets,
        })

    df = pd.DataFrame(rows).sort_values("momentum_score", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    return df


def generate_signals() -> dict:
    """Generate current allocation signals.

    Returns dict with:
      - 'holdings': list of (ticker, weight) to hold
      - 'ranking': full ranking DataFrame
      - 'regime': 'risk-on' or 'risk-off'
    """
    ranking = rank_etfs()
    eligible = ranking[ranking["above_sma200"]].head(MOMENTUM_TOP_N)

    if len(eligible) == 0:
        # Full risk-off: everything to safe haven
        return {
            "holdings": [(SAFE_HAVEN, 1.0)],
            "ranking": ranking,
            "regime": "risk-off",
        }

    # Equal-weight the eligible ETFs
    w = 1.0 / MOMENTUM_TOP_N
    holdings = [(row["ticker"], w) for _, row in eligible.iterrows()]
    # If fewer than TOP_N eligible, allocate remainder to safe haven
    remainder = 1.0 - w * len(eligible)
    if remainder > 0.01:
        holdings.append((SAFE_HAVEN, remainder))

    regime = "risk-on" if len(eligible) >= MOMENTUM_TOP_N else "mixed"
    return {
        "holdings": holdings,
        "ranking": ranking,
        "regime": regime,
    }
