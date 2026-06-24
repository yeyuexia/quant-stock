"""
Strategy 1: Dual Momentum ETF Rotation

Concept (Antonacci, 2014):
  1. Rank ETFs by composite momentum score (weighted blend of 1/3/6/12-month returns).
  2. Apply absolute momentum filter: only hold ETFs above their 200-day SMA.
  3. If fewer than N pass, allocate remainder to safe haven (T-bills).

This is a well-documented, robust strategy that works especially well with small
capital because you only trade a handful of low-cost ETFs once a month.
"""
from typing import Optional, Iterable
import pandas as pd
import numpy as np
from data import fetch_prices
from config import (
    ETF_UNIVERSE, SAFE_HAVEN, MOMENTUM_LOOKBACK_MONTHS,
    MOMENTUM_TOP_N, SMA_FILTER_PERIOD, MOMENTUM_HYSTERESIS_DEPTH,
)


def _momentum_score(prices: pd.Series, months: list[int]) -> Optional[float]:
    """Composite momentum: weighted average of N-month returns.
    Recent periods get higher weight (1/m). Returns None when no lookback
    period had enough history — was -999 sentinel before, which sorted to
    the bottom (good) but could leak into display / API as a magic number.
    """
    scores = []
    weights = []
    for m in sorted(months):
        days = m * 21  # approx trading days
        if len(prices) < days + 1:
            continue
        ret = prices.iloc[-1] / prices.iloc[-days] - 1
        w = 1.0 / m  # inverse-month weighting (recent > distant)
        scores.append(ret * w)
        weights.append(w)
    if not weights:
        return None
    return sum(scores) / sum(weights)


def rank_etfs() -> pd.DataFrame:
    """Return a DataFrame of ETFs ranked by composite momentum with SMA filter.

    Returns an empty DataFrame (with the expected columns) when no ticker
    has enough history — caller's `sort_values("momentum_score")` previously
    crashed on a column-less empty frame.
    """
    # SAFE_HAVEN was previously fetched here too, but never read — `generate_signals`
    # builds its own remainder allocation. Drop it to save one column in the
    # already-cached fetch_prices batch.
    prices = fetch_prices(ETF_UNIVERSE, period="2y")

    rows = []
    for t in ETF_UNIVERSE:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < SMA_FILTER_PERIOD:
            continue

        score = _momentum_score(s, MOMENTUM_LOOKBACK_MONTHS)
        if score is None:
            continue   # not enough history for any lookback period
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

    if not rows:
        # Empty universe — return a frame with the expected columns so
        # downstream sort/filter doesn't trip a missing-column KeyError.
        cols = ["ticker", "price", "momentum_score", "above_sma200", "rank"]
        cols += [f"{m}m_ret" for m in MOMENTUM_LOOKBACK_MONTHS]
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows).sort_values("momentum_score", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    return df


def select_with_hysteresis(
    ranking: pd.DataFrame,
    *,
    top_n: int,
    held_etfs: Optional[Iterable[str]] = None,
    hysteresis_depth: int = 0,
) -> pd.DataFrame:
    """Pick ETFs from a ranked DataFrame, applying hysteresis to held names.

    Rules:
      1. Top-`top_n` SMA-eligible rows are always selected.
      2. Rows at rank top_n+1 .. top_n+hysteresis_depth are also selected
         IF the ticker is in `held_etfs` (still in the portfolio) AND
         still above SMA. Falling out of trend overrides hysteresis.
      3. Returned DataFrame keeps the order of `ranking` (best first).

    Net effect: a held ETF needs to slip past rank `top_n + hysteresis_depth`
    OR drop below its 200-day SMA before it's actually sold. Sticky picks
    can push the total holdings above top_n; callers should weight using
    1 / max(len(eligible), top_n) to stay ≤ 100%.
    """
    sma_ok = ranking[ranking["above_sma200"]]
    top = sma_ok.head(top_n)
    if hysteresis_depth <= 0 or not held_etfs:
        return top

    held = set(held_etfs)
    sticky_window = sma_ok.iloc[top_n : top_n + hysteresis_depth]
    sticky_kept = sticky_window[sticky_window["ticker"].isin(held)]
    if sticky_kept.empty:
        return top
    return pd.concat([top, sticky_kept])


def generate_signals(held_etfs: Optional[Iterable[str]] = None) -> dict:
    """Generate current allocation signals.

    Args:
      held_etfs: set of ETF tickers currently held in the core tranche.
        Enables hysteresis — a held ETF that slips one rank below the cutoff
        is retained (see select_with_hysteresis). Pass None to disable.

    Returns dict with:
      - 'holdings': list of (ticker, weight) to hold
      - 'holdings_ranked': list of (ticker, weight, rank) — rank reflects
        the position in `ranking` (1 = best in universe), not slot index
      - 'ranking': full ranking DataFrame
      - 'regime': 'risk-on', 'mixed', or 'risk-off'
    """
    ranking = rank_etfs()
    eligible = select_with_hysteresis(
        ranking,
        top_n=MOMENTUM_TOP_N,
        held_etfs=held_etfs,
        hysteresis_depth=MOMENTUM_HYSTERESIS_DEPTH,
    )

    if len(eligible) == 0:
        return {
            "holdings": [(SAFE_HAVEN, 1.0)],
            "holdings_ranked": [(SAFE_HAVEN, 1.0, 1)],
            "ranking": ranking,
            "regime": "risk-off",
        }

    # Weight cap: 1/TOP_N. With sticky additions, eligible may exceed TOP_N;
    # divide by max() so total never overshoots 100%.
    w = 1.0 / max(len(eligible), MOMENTUM_TOP_N)
    holdings = [(row["ticker"], w) for _, row in eligible.iterrows()]
    holdings_ranked = [
        (row["ticker"], w, int(row["rank"]))
        for _, row in eligible.iterrows()
    ]
    remainder = 1.0 - w * len(eligible)
    if remainder > 0.01:
        holdings.append((SAFE_HAVEN, remainder))
        holdings_ranked.append((SAFE_HAVEN, remainder, len(eligible) + 1))

    regime = "risk-on" if len(eligible) >= MOMENTUM_TOP_N else "mixed"
    return {
        "holdings": holdings,
        "holdings_ranked": holdings_ranked,
        "ranking": ranking,
        "regime": regime,
    }
