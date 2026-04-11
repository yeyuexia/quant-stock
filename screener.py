"""
Strategy 2: Value + Quality Stock Screener

For the satellite portion of the portfolio (~20%), screen for:
  - Reasonable valuation (P/E < 20)
  - High quality (ROE > 12%, manageable debt)
  - Positive momentum (price above 50-day SMA)

This is a classic Greenblatt "magic formula" inspired approach combined with
momentum confirmation.
"""
from typing import Optional, List
import pandas as pd
from data import fetch_prices, fetch_info
from config import (
    WATCHLIST, SCREEN_MAX_PE, SCREEN_MIN_ROE,
    SCREEN_MAX_DEBT_EQUITY, SCREEN_TOP_N,
)


def screen_stocks(tickers: Optional[List[str]] = None) -> pd.DataFrame:
    """Screen stocks for value + quality. Returns ranked DataFrame."""
    if tickers is None:
        tickers = WATCHLIST

    rows = []
    for t in tickers:
        try:
            info = fetch_info(t)
        except Exception:
            continue

        pe = info.get("trailingPE") or info.get("forwardPE")
        roe = info.get("returnOnEquity")
        de = info.get("debtToEquity")
        if de is not None:
            de = de / 100  # yfinance returns as percentage
        mcap = info.get("marketCap", 0)
        div_yield = info.get("dividendYield", 0) or 0
        rev_growth = info.get("revenueGrowth", 0) or 0
        price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        name = info.get("shortName", t)

        # Compute simple momentum check
        try:
            prices = fetch_prices([t], period="6mo")
            s = prices[t].dropna()
            sma50 = s.rolling(50).mean().iloc[-1] if len(s) >= 50 else s.mean()
            above_sma50 = price > sma50 if price else False
            ret_3m = s.iloc[-1] / s.iloc[-63] - 1 if len(s) >= 63 else 0
        except Exception:
            above_sma50 = False
            ret_3m = 0

        rows.append({
            "ticker": t,
            "name": name,
            "price": price,
            "pe": pe,
            "roe": roe,
            "debt_equity": de,
            "market_cap_B": mcap / 1e9 if mcap else None,
            "div_yield": div_yield,
            "rev_growth": rev_growth,
            "above_sma50": above_sma50,
            "ret_3m": ret_3m,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Composite score: value + quality + momentum
    # Normalize each factor to [0, 1] and combine
    def _rank_norm(series, ascending=True):
        ranked = series.rank(ascending=ascending, na_option="bottom")
        return (ranked - ranked.min()) / (ranked.max() - ranked.min() + 1e-9)

    df["value_score"] = _rank_norm(df["pe"], ascending=True)          # lower PE = better
    df["quality_score"] = _rank_norm(df["roe"], ascending=False)       # higher ROE = better
    df["momentum_score"] = _rank_norm(df["ret_3m"], ascending=False)   # higher return = better
    df["growth_score"] = _rank_norm(df["rev_growth"], ascending=False)

    df["composite"] = (
        0.30 * df["value_score"]
        + 0.30 * df["quality_score"]
        + 0.25 * df["momentum_score"]
        + 0.15 * df["growth_score"]
    )

    df = df.sort_values("composite", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    return df
