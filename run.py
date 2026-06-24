#!/usr/bin/env python3
"""
Quantitative Investment System — Daily Read-Only Reporter

Tranche structure is dynamic (sized from snap.equity × pct at rebalance
time):
  Core tranche       ≈ (1 - AGGRESSIVE_TRANCHE_PCT) × system equity
                     CANSLIM stock screen + dual-momentum ETF rotation
  Aggressive tranche ≈ AGGRESSIVE_TRANCHE_PCT × system equity
                     Leveraged ETF momentum, top-N daily rebalance

Run:
  python3 run.py                          # all sections
  python3 run.py --section macro          # only macro
  python3 run.py --skip backtest          # everything except backtest
  python3 run.py --with-review            # call investor_agent (LLM cost)
  python3 run.py --backtest-years 10      # change backtest horizon

Sections (run in this order when no filter):
  macro, momentum, screener, alpaca, risk, sentiment, backtest
"""
import argparse
import sys
import numpy as np
import pandas as pd
from tabulate import tabulate

import config

# Light-weight imports OK at module level; anything that imports yfinance /
# external APIs / large deps is lazy-imported inside its section function
# so a single broken dependency doesn't kill the whole report.
from config import (
    MAX_POSITION_PCT, CASH_BUFFER_PCT, MOMENTUM_TOP_N,
    PORTFOLIO_MODE, ETF_ALLOCATION_PCT, STOCK_ALLOCATION_PCT,
    USE_LEVERAGED_ETFS, STOP_LOSS_PCT,
    AGGRESSIVE_TRANCHE_PCT, AGGRESSIVE_PARAMS,
)


# ── Display helpers ─────────────────────────────────────────────────

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


def signal_bar(s: float) -> str:
    """5-step textual bar for [-1, +1] signal values."""
    bars = ["-----", "----+", "---++", "--+++", "-++++", "+++++"]
    idx = max(0, min(5, int((s + 1) / 2 * 5)))
    return f"[{bars[idx]}]"


def safe_section(name: str, fn, *args, **kwargs):
    """Run a section function inside a uniform try/except.

    Behaviors:
      - On BrokerError or known data errors: print `(skipped: ...)` under
        the section header so partial output stays useful.
      - On other Exceptions: still print the skipped marker BUT also re-raise
        so genuine bugs aren't silently eaten. To recover-and-continue from
        an unexpected exception, the caller wraps the section with --skip.
      - Returns the function's return value (or None on caught failure).
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        # Print the skipped marker so the user sees something useful.
        print(f"  ⚠ Section '{name}' failed: {type(e).__name__}: {e}")
        # Re-raise programming bugs (AttributeError / NameError / TypeError) —
        # silently absorbing them historically masked real defects (e.g.,
        # renamed config keys not propagating). Known runtime errors that are
        # legitimately "data not available right now" should be caught BY
        # THE SECTION ITSELF and converted to a graceful print + return.
        if isinstance(e, (AttributeError, NameError, TypeError, ImportError)):
            raise
        return None


# ── Section: Macro Regime ───────────────────────────────────────────

def run_macro_regime():
    from macro import macro_regime_score, macro_risk_adjustment

    section("MACRO REGIME ANALYSIS (FRED)")
    result = macro_regime_score()
    score = result["score"]
    regime = result["regime"]

    print(f"  Composite Score: {score:+.3f}  {signal_bar(score)}")
    print(f"  Regime:          {regime.upper()}")
    print()

    for name, ind in result["indicators"].items():
        s = ind["signal"]
        label = ind["label"]
        print(f"    {name:18s} {s:+.1f}  {signal_bar(s)}  {label}")

    adj = macro_risk_adjustment(1.0)
    print(f"\n  Macro Risk Adjustment: {adj*100:.0f}% of target equity allocation")
    if adj < 0.7:
        print("  WARNING: Macro conditions suggest reducing risk exposure!")
    elif adj > 0.9:
        print("  Macro conditions support full equity allocation.")
    return result


# ── Section: Momentum ETF Rotation ──────────────────────────────────

def run_momentum_strategy():
    from momentum import generate_signals
    from config import ETF_UNIVERSE

    section("STRATEGY 1: DUAL MOMENTUM ETF ROTATION")
    base_n = config.REBALANCE_DAYS["core"]
    hyst = config.MOMENTUM_HYSTERESIS_DEPTH
    hyst_note = f" (+{hyst} hysteresis)" if hyst > 0 else ""
    print(f"  Universe: {len(ETF_UNIVERSE)} ETFs | "
          f"Hold top {MOMENTUM_TOP_N}{hyst_note} | "
          f"Rebalance every {base_n}d")
    print()

    signals = generate_signals()
    ranking = signals["ranking"]

    table_data = []
    for _, row in ranking.iterrows():
        table_data.append([
            row["rank"], row["ticker"], f"${row['price']:.2f}",
            fmt_pct(row.get("1m_ret")), fmt_pct(row.get("3m_ret")),
            fmt_pct(row.get("6m_ret")), fmt_pct(row.get("12m_ret")),
            f"{row['momentum_score']:.4f}",
            "✓" if row["above_sma200"] else "✗",
        ])

    print(tabulate(table_data,
                   headers=["#", "Ticker", "Price", "1M", "3M", "6M",
                            "12M", "Score", ">SMA200"],
                   tablefmt="simple"))

    print(f"\n  Market Regime: {signals['regime'].upper()}")
    print(f"\n  ── Recommended ETF Holdings ──")
    for ticker, weight in signals["holdings"]:
        print(f"    {ticker:6s}  {weight*100:5.1f}%")
    return signals


# ── Section: CANSLIM Stock Screener ─────────────────────────────────

def run_stock_screener(with_review: bool = False):
    from screener import screen_stocks

    section("STRATEGY 2: CANSLIM C+A+T SCREEN")
    print(f"  Fundamental pre-filter: "
          f"EPS YoY ≥{config.SCREEN_EPS_Q_GROWTH_MIN*100:.0f}%, "
          f"Rev YoY ≥{config.SCREEN_REV_GROWTH_MIN*100:.0f}%, "
          f"annual EPS growing")
    print(f"  Technical filter: "
          f"RS ≥{config.SCREEN_RS_MIN:.0f}, "
          f"ADR ≥{config.SCREEN_ADR_MIN*100:.0f}%, "
          f"above EMA{config.SCREEN_EMA_FAST}+EMA{config.SCREEN_EMA_SLOW}, "
          f"VCP base bonus\n")

    if with_review:
        df, review = screen_stocks(with_review=True)
    else:
        df = screen_stocks()
        review = None

    if df.empty:
        print("  No stocks passed all filters.")
        return df

    def _fmt_growth(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v*100:+.0f}%"

    table_data = []
    for _, row in df.iterrows():
        accel = "↑" if row.get("eps_accel") else " "
        table_data.append([
            row["rank"], row["ticker"],
            f"${row['price']:.2f}" if row["price"] else "N/A",
            f"{row['rs_score']:.0f}", fmt_pct(row["adr"]),
            "✓" if row["above_ema_fast"] else "✗",
            "✓" if row["above_ema_slow"] else "✗",
            "✓" if row["in_base"] else "✗",
            _fmt_growth(row.get("eps_q_growth")),
            _fmt_growth(row.get("rev_growth")) + accel,
            f"${row['vcp_pivot']:.2f}" if row.get("vcp_pivot") else "—",
            f"{row['composite']:.3f}",
        ])

    print(tabulate(table_data,
                   headers=["#", "Tick", "Price", "RS", "ADR",
                            f">EMA{config.SCREEN_EMA_FAST}",
                            f">EMA{config.SCREEN_EMA_SLOW}",
                            "VCP", "EPS%", "Rev%", "Pivot", "Score"],
                   tablefmt="simple"))

    if review:
        print("\n── Investor Agent Review ──")
        print(review)
    return df


# ── Section: Alpaca Holdings ────────────────────────────────────────

def run_alpaca_holdings():
    """Read-only snapshot of current Alpaca positions."""
    # Lazy import: broker.py construction can fail if env keys missing or
    # SDK unavailable — keep that contained to this section.
    from broker import Broker, BrokerError

    section("CURRENT ALPACA HOLDINGS")
    try:
        broker = Broker(env=config.ALPACA_ENV)
        acc = broker.get_account()
        positions = broker.get_positions()
    except BrokerError as e:
        print(f"  (skipped: BrokerError: {e})")
        return None

    print(f"  Env:    {broker.env}")
    print(f"  Cash:   ${acc.cash:,.2f}")
    print(f"  Equity: ${acc.equity:,.2f}")
    for p in positions:
        pnl = p.unrealized_pl
        icon = "▲" if pnl >= 0 else "▼"
        print(f"    {p.symbol:6s}  {p.qty:>8.2f} × ${p.avg_entry:>8.2f} = "
              f"${p.market_value:>10,.2f}  {icon} ${pnl:+,.2f}")
    return positions


# ── Section: Risk Analysis ──────────────────────────────────────────

def run_risk_analysis(signals):
    from risk import portfolio_stats, correlation_matrix, diversification_ratio
    from data import fetch_prices, compute_returns

    section("RISK ANALYSIS")

    # Exclude safe haven from risk math (cash/T-bill has ~0 vol; including
    # it would inflate the diversification ratio and dampen Sharpe).
    safe = config.SAFE_HAVEN
    held = [(t, w) for t, w in signals["holdings"] if t != safe]
    if not held:
        print(f"  All in safe haven ({safe}) — minimal risk.")
        return

    tickers = [t for t, _ in held]
    raw_weights = np.array([w for _, w in held], dtype=float)
    # Renormalize over non-safe-haven holdings (so the risk math reflects
    # the actual equity composition, not equal-weight assumed in the
    # previous version). Sum guaranteed > 0 here since held isn't empty
    # and signals' holdings always carry positive weights.
    weights = raw_weights / raw_weights.sum()

    prices = fetch_prices(tickers, period="1y")
    if prices.empty:
        print(f"  (skipped: no price data for {tickers})")
        return

    returns = compute_returns(prices)
    if returns.empty:
        print(f"  (skipped: returns frame empty)")
        return

    stats = portfolio_stats(returns, weights)
    div_ratio = diversification_ratio(returns, weights)

    print(f"  Weights used:")
    for t, w in zip(tickers, weights):
        print(f"    {t:6s}  {w*100:5.1f}%")
    print()
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

    if len(tickers) > 1:
        corr = correlation_matrix(returns)
        print(f"\n  ── Correlation Matrix ──")
        print(corr.round(2).to_string())


# ── Section: News & Sentiment ───────────────────────────────────────

def run_sentiment():
    from sentiment import get_market_hotspots

    section("NEWS & SOCIAL SENTIMENT")
    try:
        hotspots = get_market_hotspots()
    except Exception as e:
        print(f"  (skipped: sentiment fetch failed: {type(e).__name__}: {e})")
        return

    mood = hotspots.get("market_mood", 0)
    label = hotspots.get("mood_label", "unknown")
    news_n = hotspots.get("news_count", 0)
    reddit_n = hotspots.get("reddit_count", 0)

    bar_pos = max(0, min(20, int((mood + 1) / 2 * 20)))
    bar = "─" * bar_pos + "█" + "─" * (20 - bar_pos)
    print(f"  Market Mood: {label} ({mood:+.2f})")
    print(f"  BEAR [{bar}] BULL")
    print(f"  Sources: {news_n} news articles, {reddit_n} Reddit posts\n")

    alerts = hotspots.get("portfolio_alerts", [])
    if alerts:
        print(f"  ── Portfolio Alerts ({len(alerts)} items) ──")
        for a in alerts[:10]:
            sent = a.get("sentiment", "neutral")
            icon = "▲" if sent == "bullish" else "▼" if sent == "bearish" else "─"
            print(f"    {icon} [{a.get('ticker', '?'):5s}] {a.get('headline', '')[:65]}")
            print(f"      via {a.get('source', '?')} | {sent}")
        print()

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
                in_port, row["ticker"], row["mentions"], mood_icon,
                f"{row['bullish']}/{row['neutral']}/{row['bearish']}",
                (row["top_headline"] or "")[:50],
            ])
        print(tabulate(table_data,
                       headers=["", "Ticker", "Mentions", "Mood", "B/N/B",
                                "Top Headline"],
                       tablefmt="simple"))
        print("  ★ = in our universe")
        print()

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

    topics = hotspots.get("trending_topics", [])
    if topics:
        topic_str = ", ".join(f"{w}({c})" for w, c in topics[:12])
        print(f"  ── Trending Topics ──")
        print(f"    {topic_str}")
        print()


# ── Section: Backtest ───────────────────────────────────────────────

def run_backtest(years: int = 5):
    from backtest import backtest_momentum

    section(f"BACKTEST: DUAL MOMENTUM ({years}Y)")
    try:
        bt = backtest_momentum(years=years)
    except Exception as e:
        print(f"  (skipped: backtest failed: {type(e).__name__}: {e})")
        return
    if bt.empty:
        print("  No backtest data available.")
        return

    start_val = bt["portfolio_value"].iloc[0]
    end_val = bt["portfolio_value"].iloc[-1]
    bench_start = bt["benchmark_value"].iloc[0]
    bench_end = bt["benchmark_value"].iloc[-1]

    port_total = end_val / start_val - 1
    bench_total = bench_end / bench_start - 1

    days = (bt["date"].iloc[-1] - bt["date"].iloc[0]).days
    yrs = days / 365.25
    port_ann = (1 + port_total) ** (1 / yrs) - 1 if yrs > 0 else 0
    bench_ann = (1 + bench_total) ** (1 / yrs) - 1 if yrs > 0 else 0

    cum = bt["portfolio_value"] / bt["portfolio_value"].iloc[0]
    peak = cum.cummax()
    max_dd = ((cum - peak) / peak).min()

    bench_cum = bt["benchmark_value"] / bt["benchmark_value"].iloc[0]
    bench_peak = bench_cum.cummax()
    bench_dd = ((bench_cum - bench_peak) / bench_peak).min()

    print(f"  Period: {bt['date'].iloc[0].strftime('%Y-%m-%d')} → "
          f"{bt['date'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  Starting Capital: {fmt_dollar(config.INITIAL_CAPITAL)} "
          f"(legacy reference — production sizes tranches from live equity)")
    print()
    print(f"  {'':20s}  {'Strategy':>12s}  {'SPY B&H':>12s}")
    print(f"  {'─'*20}  {'─'*12}  {'─'*12}")
    print(f"  {'Final Value':20s}  {fmt_dollar(end_val):>12s}  "
          f"{fmt_dollar(bench_end):>12s}")
    print(f"  {'Total Return':20s}  {fmt_pct(port_total):>12s}  "
          f"{fmt_pct(bench_total):>12s}")
    print(f"  {'Ann. Return':20s}  {fmt_pct(port_ann):>12s}  "
          f"{fmt_pct(bench_ann):>12s}")
    print(f"  {'Max Drawdown':20s}  {fmt_pct(max_dd):>12s}  "
          f"{fmt_pct(bench_dd):>12s}")


# ── CLI dispatch ────────────────────────────────────────────────────

ALL_SECTIONS = ("macro", "momentum", "screener", "alpaca", "risk",
                "sentiment", "backtest")


def _now_et_str() -> str:
    """ET wall-clock for the report header. Falls back to local if zoneinfo
    isn't available — pinned via timeutils.now_et which raises in that case,
    so any failure here is informative."""
    try:
        from timeutils import now_et
        return now_et().strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M (local)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section", choices=ALL_SECTIONS, default=None,
                    help="run only this single section")
    ap.add_argument("--skip", action="append", choices=ALL_SECTIONS, default=[],
                    help="skip this section (may be repeated)")
    ap.add_argument("--with-review", action="store_true",
                    help="run investor_agent LLM review during the screener "
                         "section (costs API tokens; off by default)")
    ap.add_argument("--backtest-years", type=int, default=5,
                    help="backtest horizon in years (default 5)")
    args = ap.parse_args(argv)

    # Section filter
    if args.section:
        wanted = (args.section,)
    else:
        wanted = tuple(s for s in ALL_SECTIONS if s not in args.skip)

    mode_display = {
        "conservative": "CONSERVATIVE — Capital preservation, wide diversification",
        "balanced":     "BALANCED — ETF rotation + stock picks",
        "growth":       "GROWTH — Aggressive, leveraged ETFs + small/mid-cap stocks",
    }
    mode_str = mode_display.get(PORTFOLIO_MODE, PORTFOLIO_MODE)

    print("╔════════════════════════════════════════════════════════════╗")
    print("║        QUANTITATIVE INVESTMENT SYSTEM                    ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print(f"  Date:    {_now_et_str()}")
    print(f"  Mode:    {mode_str}")
    if USE_LEVERAGED_ETFS:
        print(f"  ⚡ Leveraged ETFs ENABLED ({', '.join(config.ETF_LEVERAGED)})")
    print(f"  ETF/Stock Split: {ETF_ALLOCATION_PCT*100:.0f}% / "
          f"{STOCK_ALLOCATION_PCT*100:.0f}%")
    print(f"  Rebalance: core every {config.REBALANCE_DAYS['core']}d / "
          f"aggressive every {config.REBALANCE_DAYS['aggressive']}d | "
          f"Stop-loss ceiling: {STOP_LOSS_PCT*100:.0f}%")
    print(f"  Sections: {', '.join(wanted)}")

    # We collect signals from the momentum section to feed risk analysis.
    signals_ref = {"signals": None}

    def maybe_run(name: str, fn, *args, **kwargs):
        if name not in wanted:
            return None
        return safe_section(name, fn, *args, **kwargs)

    maybe_run("macro", run_macro_regime)

    signals = maybe_run("momentum", run_momentum_strategy)
    if signals is not None:
        signals_ref["signals"] = signals

    maybe_run("screener", run_stock_screener, with_review=args.with_review)
    maybe_run("alpaca", run_alpaca_holdings)

    # Risk needs signals; if momentum was skipped or failed, skip risk too.
    if "risk" in wanted:
        if signals_ref["signals"] is None:
            section("RISK ANALYSIS")
            print("  (skipped: needs momentum signals; "
                  "run momentum or remove --skip momentum)")
        else:
            safe_section("risk", run_risk_analysis, signals_ref["signals"])

    maybe_run("sentiment", run_sentiment)
    maybe_run("backtest", run_backtest, years=args.backtest_years)

    section("NEXT STEPS")
    print("  Recommendations above are read-only. To act on them:")
    print(f"    python3 rebalancer.py --tranche core --dry-run")
    print(f"    python3 rebalancer.py --tranche aggressive --dry-run")
    print(f"    # remove --dry-run when ready")
    print()


if __name__ == "__main__":
    main()
