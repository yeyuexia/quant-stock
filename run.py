#!/usr/bin/env python3
"""
Quantitative Investment System — Main Runner
$5,000 Portfolio | Dual Momentum + Value Screening

Run: python3 run.py
"""
import sys
import numpy as np
import pandas as pd
from tabulate import tabulate

from config import (INITIAL_CAPITAL, MAX_POSITION_PCT, CASH_BUFFER_PCT, MOMENTUM_TOP_N,
                     PORTFOLIO_MODE, ETF_ALLOCATION_PCT, STOCK_ALLOCATION_PCT,
                     USE_LEVERAGED_ETFS, REBALANCE_FREQUENCY_DAYS, STOP_LOSS_PCT)
from momentum import generate_signals
from screener import screen_stocks
from risk import portfolio_stats, position_size, correlation_matrix, diversification_ratio
from data import fetch_prices, compute_returns
from backtest import backtest_momentum
from macro import macro_regime_score, macro_risk_adjustment
from sentiment import get_market_hotspots


def fmt_pct(v, digits=1):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "N/A"
    return f"{v*100:+.{digits}f}%"


def fmt_dollar(v):
    return f"${v:,.0f}"


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def run_macro_regime():
    section("MACRO REGIME ANALYSIS (FRED)")
    result = macro_regime_score()

    score = result["score"]
    regime = result["regime"]

    # Signal bar visualization
    def signal_bar(s):
        bars = ["-----", "----+", "---++", "--+++", "-++++", "+++++"]
        idx = int((s + 1) / 2 * 5)
        idx = max(0, min(5, idx))
        return f"[{bars[idx]}]"

    print(f"  Composite Score: {score:+.3f}  {signal_bar(score)}")
    print(f"  Regime:          {regime.upper()}")
    print()

    for name, ind in result["indicators"].items():
        s = ind["signal"]
        label = ind["label"]
        bar = signal_bar(s)
        print(f"    {name:18s} {s:+.1f}  {bar}  {label}")

    # Risk adjustment recommendation
    adj = macro_risk_adjustment(1.0)
    print(f"\n  Macro Risk Adjustment: {adj*100:.0f}% of target equity allocation")
    if adj < 0.7:
        print("  WARNING: Macro conditions suggest reducing risk exposure!")
    elif adj > 0.9:
        print("  Macro conditions support full equity allocation.")

    return result


def run_momentum_strategy():
    section("STRATEGY 1: DUAL MOMENTUM ETF ROTATION")
    from config import ETF_UNIVERSE
    print(f"  Universe: {len(ETF_UNIVERSE)} ETFs | Hold top {MOMENTUM_TOP_N} | Monthly rebalance")
    print()

    signals = generate_signals()
    ranking = signals["ranking"]

    # Display ranking
    display_cols = ["rank", "ticker", "price", "1m_ret", "3m_ret", "6m_ret",
                    "12m_ret", "momentum_score", "above_sma200"]
    table_data = []
    for _, row in ranking.iterrows():
        table_data.append([
            row["rank"],
            row["ticker"],
            f"${row['price']:.2f}",
            fmt_pct(row.get("1m_ret")),
            fmt_pct(row.get("3m_ret")),
            fmt_pct(row.get("6m_ret")),
            fmt_pct(row.get("12m_ret")),
            f"{row['momentum_score']:.4f}",
            "✓" if row["above_sma200"] else "✗",
        ])

    print(tabulate(table_data,
                   headers=["#", "Ticker", "Price", "1M", "3M", "6M", "12M", "Score", ">SMA200"],
                   tablefmt="simple"))

    print(f"\n  Market Regime: {signals['regime'].upper()}")
    print(f"\n  ── Recommended ETF Holdings ──")
    for ticker, weight in signals["holdings"]:
        print(f"    {ticker:6s}  {weight*100:5.1f}%")

    return signals


def run_stock_screener():
    section("STRATEGY 2: VALUE + QUALITY STOCK SCREEN")
    print("  Screening watchlist for value, quality, momentum, growth...\n")

    df = screen_stocks()
    if df.empty:
        print("  No data available.")
        return df

    table_data = []
    for _, row in df.head(15).iterrows():
        table_data.append([
            row["rank"],
            row["ticker"],
            row.get("name", "")[:20],
            f"${row['price']:.2f}" if row["price"] else "N/A",
            f"{row['pe']:.1f}" if row["pe"] else "N/A",
            fmt_pct(row["roe"]) if row["roe"] else "N/A",
            f"{row['debt_equity']:.2f}" if row["debt_equity"] else "N/A",
            fmt_pct(row["div_yield"]),
            fmt_pct(row["rev_growth"]),
            fmt_pct(row["ret_3m"]),
            f"{row['composite']:.3f}",
        ])

    print(tabulate(table_data,
                   headers=["#", "Tick", "Name", "Price", "P/E", "ROE", "D/E",
                            "Div%", "RevGr", "3M Ret", "Score"],
                   tablefmt="simple"))
    return df


def run_portfolio_construction(signals, screen_df, macro=None):
    section("PORTFOLIO CONSTRUCTION")
    capital = INITIAL_CAPITAL
    cash_reserve = capital * CASH_BUFFER_PCT
    investable = capital - cash_reserve

    # Macro adjustment: scale equity allocation based on macro regime
    macro_adj = 1.0
    if macro:
        macro_adj = macro_risk_adjustment(1.0)

    base_etf_pct = ETF_ALLOCATION_PCT
    base_stock_pct = STOCK_ALLOCATION_PCT
    adj_etf_pct = base_etf_pct * macro_adj
    adj_stock_pct = base_stock_pct * macro_adj
    safe_pct = 1.0 - adj_etf_pct - adj_stock_pct  # remainder to safety

    etf_alloc = investable * adj_etf_pct
    stock_alloc = investable * adj_stock_pct
    safe_alloc = investable * safe_pct

    print(f"  Total Capital:    {fmt_dollar(capital)}")
    print(f"  Cash Reserve:     {fmt_dollar(cash_reserve)} ({CASH_BUFFER_PCT*100:.0f}%)")
    if macro_adj < 1.0:
        print(f"  Macro Adjustment: {macro_adj*100:.0f}% (reduced due to macro conditions)")
    print(f"  ETF Allocation:   {fmt_dollar(etf_alloc)} ({adj_etf_pct*100:.0f}%)")
    print(f"  Stock Allocation: {fmt_dollar(stock_alloc)} ({adj_stock_pct*100:.0f}%)")
    if safe_pct > 0.01:
        print(f"  Safety (BIL):     {fmt_dollar(safe_alloc)} ({safe_pct*100:.0f}%) ← macro hedge")

    # ETF positions
    print(f"\n  ── ETF Positions ──")
    etf_positions = []
    for ticker, weight in signals["holdings"]:
        dollars = etf_alloc * weight
        # Get current price
        try:
            from data import fetch_info
            info = fetch_info(ticker)
            price = info.get("currentPrice") or info.get("regularMarketPrice", 100)
        except Exception:
            price = 100
        shares = int(dollars / price) if price > 0 else 0
        actual_cost = shares * price
        etf_positions.append((ticker, shares, price, actual_cost))
        print(f"    {ticker:6s}  {shares:4d} shares × ${price:>8.2f} = ${actual_cost:>8.2f}")

    # Stock positions — pick affordable stocks that fit the budget
    print(f"\n  ── Stock Positions ──")
    stock_positions = []
    if screen_df is not None and not screen_df.empty:
        # Greedily pick top-ranked stocks we can afford
        remaining_budget = stock_alloc
        for _, row in screen_df.iterrows():
            if len(stock_positions) >= 3:
                break
            price = row["price"] if row["price"] else 0
            if price <= 0 or price > remaining_budget:
                continue
            shares = int(remaining_budget / max(2, 3 - len(stock_positions)) / price)
            if shares == 0:
                shares = 1  # at least 1 share if we can afford it
            if shares * price > remaining_budget:
                continue
            actual_cost = shares * price
            stock_positions.append((row["ticker"], shares, price, actual_cost))
            remaining_budget -= actual_cost
            print(f"    {row['ticker']:6s}  {shares:4d} shares × ${price:>8.2f} = ${actual_cost:>8.2f}")

    # Summary
    total_invested = sum(x[3] for x in etf_positions) + sum(x[3] for x in stock_positions)
    remaining_cash = capital - total_invested
    print(f"\n  Total Invested:   {fmt_dollar(total_invested)}")
    print(f"  Remaining Cash:   {fmt_dollar(remaining_cash)}")
    print(f"  Utilization:      {total_invested/capital*100:.1f}%")

    return etf_positions, stock_positions


def run_risk_analysis(signals):
    section("RISK ANALYSIS")
    tickers = [t for t, _ in signals["holdings"] if t != "BIL"]
    if not tickers:
        print("  All in safe haven — minimal risk.")
        return

    prices = fetch_prices(tickers, period="1y")
    returns = compute_returns(prices)
    weights = np.array([1.0 / len(tickers)] * len(tickers))

    stats = portfolio_stats(returns, weights)
    div_ratio = diversification_ratio(returns, weights)

    print(f"  Annualized Return:   {fmt_pct(stats['ann_return'])}")
    print(f"  Annualized Vol:      {fmt_pct(stats['ann_volatility'])}")
    print(f"  Sharpe Ratio:        {stats['sharpe_ratio']:.2f}")
    print(f"  Max Drawdown:        {fmt_pct(stats['max_drawdown'])}")
    print(f"  Daily VaR (95%):     {fmt_pct(stats['var_95_daily'])}")
    print(f"  Daily CVaR (95%):    {fmt_pct(stats['cvar_95_daily'])}")
    print(f"  Win Rate:            {fmt_pct(stats['win_rate'])}")
    print(f"  Best Day:            {fmt_pct(stats['best_day'])}")
    print(f"  Worst Day:           {fmt_pct(stats['worst_day'])}")
    print(f"  Diversification:     {div_ratio:.2f}x")

    # Correlation
    if len(tickers) > 1:
        corr = correlation_matrix(returns)
        print(f"\n  ── Correlation Matrix ──")
        print(corr.round(2).to_string())


def run_sentiment():
    section("NEWS & SOCIAL SENTIMENT")
    try:
        hotspots = get_market_hotspots()
    except Exception as e:
        print(f"  Error fetching sentiment data: {e}")
        return

    mood = hotspots["market_mood"]
    label = hotspots["mood_label"]
    news_n = hotspots["news_count"]
    reddit_n = hotspots["reddit_count"]

    # Mood bar
    bar_pos = int((mood + 1) / 2 * 20)
    bar_pos = max(0, min(20, bar_pos))
    bar = "─" * bar_pos + "█" + "─" * (20 - bar_pos)
    print(f"  Market Mood: {label} ({mood:+.2f})")
    print(f"  BEAR [{bar}] BULL")
    print(f"  Sources: {news_n} news articles, {reddit_n} Reddit posts\n")

    # Portfolio alerts
    alerts = hotspots.get("portfolio_alerts", [])
    if alerts:
        print(f"  ── Portfolio Alerts ({len(alerts)} items) ──")
        for a in alerts[:10]:
            sent = a["sentiment"]
            icon = "▲" if sent == "bullish" else "▼" if sent == "bearish" else "─"
            print(f"    {icon} [{a['ticker']:5s}] {a['headline'][:65]}")
            print(f"      via {a['source']} | {sent}")
        print()

    # Ticker buzz
    buzz = hotspots.get("ticker_buzz")
    if buzz is not None and not buzz.empty:
        print(f"  ── Ticker Buzz (most mentioned) ──")
        table_data = []
        for _, row in buzz.head(12).iterrows():
            sent = row["avg_sentiment"]
            if sent > 0.15:
                mood_icon = "▲ bull"
            elif sent < -0.15:
                mood_icon = "▼ bear"
            else:
                mood_icon = "─ neut"
            in_port = "★" if row["in_portfolio"] else " "
            table_data.append([
                in_port,
                row["ticker"],
                row["mentions"],
                mood_icon,
                f"{row['bullish']}/{row['neutral']}/{row['bearish']}",
                row["top_headline"][:50] if row["top_headline"] else "",
            ])

        print(tabulate(table_data,
                       headers=["", "Ticker", "Mentions", "Mood", "B/N/B", "Top Headline"],
                       tablefmt="simple"))
        print("  ★ = in our universe")
        print()

    # Top Reddit discussions
    top_reddit = hotspots.get("top_reddit", [])
    if top_reddit:
        print(f"  ── Hot Reddit Discussions ──")
        for p in top_reddit[:8]:
            score = p.get("score", 0)
            comments = p.get("num_comments", 0)
            sent = p.get("sentiment", "neutral")
            icon = "▲" if sent == "bullish" else "▼" if sent == "bearish" else "─"
            sub = p.get("source", "")
            title = p.get("title", "")[:70]
            print(f"    {icon} [{sub:18s}] ↑{score:>5d} 💬{comments:>4d}  {title}")
        print()

    # Trending topics
    topics = hotspots.get("trending_topics", [])
    if topics:
        topic_str = ", ".join(f"{w}({c})" for w, c in topics[:12])
        print(f"  ── Trending Topics ──")
        print(f"    {topic_str}")
        print()


def run_backtest():
    section("BACKTEST: DUAL MOMENTUM (5Y)")
    try:
        bt = backtest_momentum(years=5)
        if bt.empty:
            print("  No backtest data available.")
            return

        start_val = bt["portfolio_value"].iloc[0]
        end_val = bt["portfolio_value"].iloc[-1]
        bench_start = bt["benchmark_value"].iloc[0]
        bench_end = bt["benchmark_value"].iloc[-1]

        port_total = end_val / start_val - 1
        bench_total = bench_end / bench_start - 1

        # Annualize
        days = (bt["date"].iloc[-1] - bt["date"].iloc[0]).days
        years = days / 365.25
        port_ann = (1 + port_total) ** (1 / years) - 1 if years > 0 else 0
        bench_ann = (1 + bench_total) ** (1 / years) - 1 if years > 0 else 0

        # Max drawdown
        cum = bt["portfolio_value"] / bt["portfolio_value"].iloc[0]
        peak = cum.cummax()
        max_dd = ((cum - peak) / peak).min()

        bench_cum = bt["benchmark_value"] / bt["benchmark_value"].iloc[0]
        bench_peak = bench_cum.cummax()
        bench_dd = ((bench_cum - bench_peak) / bench_peak).min()

        print(f"  Period: {bt['date'].iloc[0].strftime('%Y-%m-%d')} → {bt['date'].iloc[-1].strftime('%Y-%m-%d')}")
        print(f"  Starting Capital: {fmt_dollar(INITIAL_CAPITAL)}")
        print()
        print(f"  {'':20s}  {'Strategy':>12s}  {'SPY B&H':>12s}")
        print(f"  {'─'*20}  {'─'*12}  {'─'*12}")
        print(f"  {'Final Value':20s}  {fmt_dollar(end_val):>12s}  {fmt_dollar(bench_end):>12s}")
        print(f"  {'Total Return':20s}  {fmt_pct(port_total):>12s}  {fmt_pct(bench_total):>12s}")
        print(f"  {'Ann. Return':20s}  {fmt_pct(port_ann):>12s}  {fmt_pct(bench_ann):>12s}")
        print(f"  {'Max Drawdown':20s}  {fmt_pct(max_dd):>12s}  {fmt_pct(bench_dd):>12s}")

    except Exception as e:
        print(f"  Backtest error: {e}")


def main():
    mode_display = {
        "conservative": "CONSERVATIVE — Capital preservation, wide diversification",
        "balanced": "BALANCED — ETF rotation + stock picks",
        "growth": "GROWTH — Aggressive, leveraged ETFs + small/mid-cap stocks",
    }
    mode_str = mode_display.get(PORTFOLIO_MODE, PORTFOLIO_MODE)

    print("╔════════════════════════════════════════════════════════════╗")
    print("║        QUANTITATIVE INVESTMENT SYSTEM                    ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print(f"  Date:    {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Capital: {fmt_dollar(INITIAL_CAPITAL)}")
    print(f"  Mode:    {mode_str}")
    if USE_LEVERAGED_ETFS:
        print(f"  ⚡ Leveraged ETFs ENABLED (TQQQ, SOXL, UPRO, TNA, TECL, LABU)")
    print(f"  ETF/Stock Split: {ETF_ALLOCATION_PCT*100:.0f}% / {STOCK_ALLOCATION_PCT*100:.0f}%")
    print(f"  Rebalance: every {REBALANCE_FREQUENCY_DAYS} days | Stop-loss: {STOP_LOSS_PCT*100:.0f}%")

    # 0. Macro regime
    macro = run_macro_regime()

    # 1. Momentum ETF strategy
    signals = run_momentum_strategy()

    # 2. Stock screener
    screen_df = run_stock_screener()

    # 3. Portfolio construction (macro-adjusted)
    run_portfolio_construction(signals, screen_df, macro)

    # 4. Risk analysis
    run_risk_analysis(signals)

    # 5. News & Social Sentiment
    run_sentiment()

    # 6. Backtest
    run_backtest()

    section("NEXT STEPS")
    print("  1. Review the recommended allocation above")
    print("  2. Place orders through your broker")
    print("  3. Set stop-loss orders at -8% per position")
    print("  4. Re-run this system monthly to rebalance")
    print("  5. Monitor regime changes (risk-on → risk-off)")
    print()


if __name__ == "__main__":
    main()
