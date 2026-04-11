"""
Simple backtester for the dual momentum ETF rotation strategy.

Runs monthly rebalancing over historical data and computes performance metrics.
"""
import pandas as pd
import numpy as np
from data import fetch_prices
from config import (
    ETF_UNIVERSE, SAFE_HAVEN, MOMENTUM_LOOKBACK_MONTHS,
    MOMENTUM_TOP_N, SMA_FILTER_PERIOD, INITIAL_CAPITAL,
    TRANSACTION_COST_BPS,
)


def _momentum_score(prices: pd.Series, end_idx: int, months: list[int]) -> float:
    scores, weights = [], []
    for m in sorted(months):
        days = m * 21
        start_idx = end_idx - days
        if start_idx < 0:
            continue
        ret = prices.iloc[end_idx] / prices.iloc[start_idx] - 1
        w = 1.0 / m
        scores.append(ret * w)
        weights.append(w)
    return sum(scores) / sum(weights) if weights else -999


def backtest_momentum(years: int = 5) -> pd.DataFrame:
    """Backtest dual momentum strategy.

    Returns DataFrame with columns: date, portfolio_value, benchmark_value, holdings
    """
    tickers = ETF_UNIVERSE + [SAFE_HAVEN]
    prices = fetch_prices(tickers, period=f"{years}y")
    prices = prices.dropna(how="all")

    # Benchmark: SPY buy-and-hold
    spy = prices["SPY"].dropna()

    # Monthly rebalance dates (last trading day of each month)
    monthly = prices.resample("ME").last().index
    # Need enough history for lookback
    min_history = max(MOMENTUM_LOOKBACK_MONTHS) * 21
    start_idx = min_history + SMA_FILTER_PERIOD

    if len(prices) < start_idx:
        raise ValueError(f"Not enough data: need {start_idx} days, have {len(prices)}")

    results = []
    capital = INITIAL_CAPITAL
    benchmark_capital = INITIAL_CAPITAL
    holdings = {}  # ticker -> shares
    benchmark_shares = INITIAL_CAPITAL / spy.iloc[0] if spy.iloc[0] > 0 else 0

    prev_month = None
    for i in range(start_idx, len(prices)):
        date = prices.index[i]
        current_month = date.to_period("M")

        # Track portfolio value daily
        port_val = 0
        for t, shares in holdings.items():
            if t in prices.columns and not np.isnan(prices[t].iloc[i]):
                port_val += shares * prices[t].iloc[i]
        if not holdings:
            port_val = capital

        bench_val = benchmark_shares * spy.iloc[i] if i < len(spy) else benchmark_capital

        results.append({
            "date": date,
            "portfolio_value": port_val,
            "benchmark_value": bench_val,
        })

        # Rebalance on month change
        if prev_month is not None and current_month != prev_month:
            # Sell everything
            capital = 0
            for t, shares in holdings.items():
                if t in prices.columns and not np.isnan(prices[t].iloc[i]):
                    capital += shares * prices[t].iloc[i]
            if not holdings:
                capital = port_val

            # Transaction costs on sell
            capital *= (1 - TRANSACTION_COST_BPS / 10000)

            # Rank ETFs
            scored = []
            for t in ETF_UNIVERSE:
                if t not in prices.columns:
                    continue
                s = prices[t].iloc[:i+1].dropna()
                if len(s) < SMA_FILTER_PERIOD:
                    continue
                score = _momentum_score(s, len(s) - 1, MOMENTUM_LOOKBACK_MONTHS)
                sma = s.rolling(SMA_FILTER_PERIOD).mean().iloc[-1]
                above_sma = s.iloc[-1] > sma
                scored.append((t, score, above_sma))

            scored.sort(key=lambda x: x[1], reverse=True)
            eligible = [(t, sc) for t, sc, above in scored if above][:MOMENTUM_TOP_N]

            # Allocate
            holdings = {}
            if eligible:
                w = capital / MOMENTUM_TOP_N
                for t, _ in eligible:
                    p = prices[t].iloc[i]
                    if p > 0:
                        shares = int(w / p)
                        if shares > 0:
                            holdings[t] = shares
                # Remainder to safe haven
                invested = sum(holdings[t] * prices[t].iloc[i] for t in holdings)
                remaining = capital - invested
                if remaining > 0 and SAFE_HAVEN in prices.columns:
                    sh_price = prices[SAFE_HAVEN].iloc[i]
                    if sh_price > 0:
                        holdings[SAFE_HAVEN] = int(remaining / sh_price)
            else:
                # All to safe haven
                if SAFE_HAVEN in prices.columns:
                    sh_price = prices[SAFE_HAVEN].iloc[i]
                    if sh_price > 0:
                        holdings[SAFE_HAVEN] = int(capital / sh_price)

            # Transaction costs on buy
            # (simplified: already deducted on sell side)

        prev_month = current_month

    df = pd.DataFrame(results)
    if not df.empty:
        df["portfolio_return"] = df["portfolio_value"].pct_change()
        df["benchmark_return"] = df["benchmark_value"].pct_change()
    return df
