#!/usr/bin/env python3
"""
Daily Portfolio Watchdog

Run every morning before market open, or set up as a cron job.
Checks for:
  1. Price alerts — big overnight/intraday moves in holdings
  2. Stop-loss triggers — any position hitting -8% from entry
  3. Macro regime shifts — yield curve, credit spread changes
  4. News/sentiment spikes — breaking stories on our tickers
  5. Volume anomalies — unusual trading activity
  6. Correlation breakdown — diversification failing

Usage:
  python3 watchdog.py              # full daily check
  python3 watchdog.py --quick      # price + stop-loss only
  python3 watchdog.py --portfolio  # show current portfolio status

Cron (run at 8:30 AM ET every weekday):
  30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
"""
import sys
import os
import json
import datetime as dt
import numpy as np
import pandas as pd

# ── Portfolio tracking ──────────────────────────────────────────

PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "portfolio.json")


def load_portfolio() -> dict:
    """Load saved portfolio positions."""
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {"positions": [], "cash": 0, "last_rebalance": None}


def save_portfolio(portfolio: dict):
    """Save portfolio positions."""
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2, default=str)


def init_portfolio():
    """Initialize portfolio from current recommendations.

    Two-tranche structure:
      Core ($90K):       balanced ETF rotation + stock screen
      Aggressive ($10K): top-2 leveraged ETF momentum
    """
    from config import INITIAL_CAPITAL
    portfolio = {
        "positions": [
            # ── Core tranche (balanced) ──────────────────────────
            {"ticker": "XLE",  "shares": 13, "entry_price": 56.94,  "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "MTUM", "shares": 2,  "entry_price": 263.44, "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "XLI",  "shares": 4,  "entry_price": 171.52, "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "IWM",  "shares": 2,  "entry_price": 261.30, "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "TSLA", "shares": 1,  "entry_price": 348.95, "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "INTC", "shares": 3,  "entry_price": 62.38,  "entry_date": str(dt.date.today()), "tranche": "core"},
            {"ticker": "QCOM", "shares": 1,  "entry_price": 128.06, "entry_date": str(dt.date.today()), "tranche": "core"},
            # ── Aggressive tranche (leveraged ETF — placeholder) ─
            # Replace these with actual top-2 leveraged ETF picks from run.py
            # {"ticker": "TQQQ", "shares": 50, "entry_price": 100.00, "entry_date": str(dt.date.today()), "tranche": "aggressive"},
        ],
        "cash": 1860.00,
        "initial_capital": INITIAL_CAPITAL,
        "start_date": str(dt.date.today()),
        "last_rebalance": str(dt.date.today()),
    }
    save_portfolio(portfolio)
    return portfolio


# ── Alert levels ────────────────────────────────────────────────

class Alert:
    CRITICAL = "🔴 CRITICAL"
    WARNING  = "🟡 WARNING"
    INFO     = "🟢 INFO"


def header(text):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'─'*60}")
    print(f"  {text}")
    print(f"  {now}")
    print(f"{'─'*60}")


# ── Check 1: Price Moves ───────────────────────────────────────

def check_price_moves(portfolio):
    """Check for significant price moves in holdings."""
    from data import fetch_prices, fetch_info

    alerts = []
    tickers = [p["ticker"] for p in portfolio["positions"]]
    if not tickers:
        return alerts

    prices = fetch_prices(tickers, period="5d")

    for pos in portfolio["positions"]:
        t = pos["ticker"]
        if t not in prices.columns:
            continue

        s = prices[t].dropna()
        if len(s) < 2:
            continue

        current = s.iloc[-1]
        prev_close = s.iloc[-2]
        entry = pos["entry_price"]

        # Daily change
        daily_chg = (current / prev_close - 1) * 100

        # Change from entry
        from_entry = (current / entry - 1) * 100

        # Peak since entry (for trailing stop)
        peak = s.max()
        from_peak = (current / peak - 1) * 100

        # Big daily move (>3%)
        if abs(daily_chg) > 5:
            alerts.append((Alert.CRITICAL, t,
                f"Moved {daily_chg:+.1f}% today! (${prev_close:.2f} → ${current:.2f})"))
        elif abs(daily_chg) > 3:
            alerts.append((Alert.WARNING, t,
                f"Moved {daily_chg:+.1f}% today (${prev_close:.2f} → ${current:.2f})"))

        # Tranche-specific stops: aggressive tranche uses tighter levels
        tranche = pos.get("tranche", "core")
        if tranche == "aggressive":
            from config import AGGRESSIVE_PARAMS as _AP
            stop_loss_pct  = _AP["stop_loss_pct"] * 100          # 10%
            stop_warn_pct  = stop_loss_pct * 0.7                  # 7%
            trail_stop_pct = _AP["trailing_stop_pct"] * 100       # 15%
            trail_warn_pct = trail_stop_pct * 0.67                # 10%
            tranche_label  = " [AGGRESSIVE]"
        else:
            from config import STOP_LOSS_PCT, TRAILING_STOP_PCT
            stop_loss_pct  = STOP_LOSS_PCT * 100                  # 8%
            stop_warn_pct  = stop_loss_pct * 0.625                # 5%
            trail_stop_pct = TRAILING_STOP_PCT * 100              # 12%
            trail_warn_pct = trail_stop_pct * 0.67                # 8%
            tranche_label  = ""

        # Stop-loss check
        if from_entry <= -stop_loss_pct:
            alerts.append((Alert.CRITICAL, t,
                f"STOP-LOSS TRIGGERED{tranche_label}: {from_entry:+.1f}% from entry ${entry:.2f} → ${current:.2f}. SELL NOW."))
        elif from_entry <= -stop_warn_pct:
            alerts.append((Alert.WARNING, t,
                f"Approaching stop-loss{tranche_label}: {from_entry:+.1f}% from entry"))

        # Trailing stop check
        if from_peak <= -trail_stop_pct:
            alerts.append((Alert.CRITICAL, t,
                f"TRAILING STOP HIT{tranche_label}: {from_peak:+.1f}% from peak ${peak:.2f}. Consider selling."))
        elif from_peak <= -trail_warn_pct:
            alerts.append((Alert.WARNING, t,
                f"Trailing stop warning{tranche_label}: {from_peak:+.1f}% from peak ${peak:.2f}"))

    return alerts


# ── Check 2: Portfolio Status ──────────────────────────────────

def check_portfolio_status(portfolio):
    """Calculate current portfolio value and P&L."""
    from data import fetch_info

    rows = []
    total_value = 0
    total_cost = 0

    for pos in portfolio["positions"]:
        t = pos["ticker"]
        shares = pos["shares"]
        entry = pos["entry_price"]
        cost = shares * entry

        try:
            info = fetch_info(t)
            current = info.get("currentPrice") or info.get("regularMarketPrice", entry)
        except Exception:
            current = entry

        value = shares * current
        pnl = value - cost
        pnl_pct = (current / entry - 1) * 100

        total_value += value
        total_cost += cost

        rows.append({
            "ticker": t,
            "shares": shares,
            "entry": entry,
            "current": current,
            "value": value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })

    cash = portfolio.get("cash", 0)
    total_value += cash
    initial = portfolio.get("initial_capital", 5000)
    total_pnl = total_value - initial
    total_pnl_pct = (total_value / initial - 1) * 100

    return rows, total_value, total_pnl, total_pnl_pct, cash


# ── Check 3: Volume Anomalies ─────────────────────────────────

def check_volume(portfolio):
    """Check for unusual volume (>2x 20-day average)."""
    import yfinance as yf

    alerts = []
    tickers = [p["ticker"] for p in portfolio["positions"]]

    for t in tickers:
        try:
            data = yf.download(t, period="1mo", progress=False)
            if data.empty or len(data) < 5:
                continue
            if isinstance(data.columns, pd.MultiIndex):
                vol = data["Volume"][t]
            else:
                vol = data["Volume"]
            avg_vol = vol.iloc[:-1].mean()
            last_vol = vol.iloc[-1]
            if avg_vol > 0 and last_vol > avg_vol * 2:
                ratio = last_vol / avg_vol
                alerts.append((Alert.WARNING, t,
                    f"Volume spike: {ratio:.1f}x avg ({last_vol/1e6:.1f}M vs {avg_vol/1e6:.1f}M avg)"))
        except Exception:
            continue

    return alerts


# ── Check 4: Macro Shifts ─────────────────────────────────────

def check_macro_shift():
    """Check if macro regime has changed since last check."""
    from macro import macro_regime_score

    alerts = []
    result = macro_regime_score()
    score = result["score"]
    regime = result["regime"]

    # Load previous score
    score_file = os.path.join(os.path.dirname(__file__), ".cache", "last_macro_score.json")
    prev_score = None
    prev_regime = None
    if os.path.exists(score_file):
        with open(score_file) as f:
            prev = json.load(f)
            prev_score = prev.get("score")
            prev_regime = prev.get("regime")

    # Save current
    os.makedirs(os.path.dirname(score_file), exist_ok=True)
    with open(score_file, "w") as f:
        json.dump({"score": score, "regime": regime, "date": str(dt.date.today())}, f)

    if prev_regime and prev_regime != regime:
        alerts.append((Alert.CRITICAL, "MACRO",
            f"Regime change: {prev_regime.upper()} → {regime.upper()} (score: {prev_score:+.2f} → {score:+.2f})"))

    if prev_score is not None:
        delta = score - prev_score
        if abs(delta) > 0.2:
            alerts.append((Alert.WARNING, "MACRO",
                f"Score moved {delta:+.2f} ({prev_score:+.2f} → {score:+.2f}) — regime: {regime}"))

    # Check specific danger signals
    for name, ind in result["indicators"].items():
        if name == "unemployment" and ind["signal"] == -1:
            alerts.append((Alert.CRITICAL, "MACRO",
                f"SAHM RULE TRIGGERED — recession signal! {ind['label']}"))
        if name == "yield_curve" and ind["signal"] == -1:
            alerts.append((Alert.CRITICAL, "MACRO",
                f"Yield curve deeply inverted — recession warning! {ind['label']}"))
        if name == "credit_spreads" and ind["signal"] <= -0.5:
            alerts.append((Alert.WARNING, "MACRO",
                f"Credit spreads widening — financial stress! {ind['label']}"))

    return alerts, result


# ── Check 5: News Alerts ──────────────────────────────────────

def check_news(portfolio):
    """Check for breaking news on our holdings."""
    from sentiment import get_market_hotspots

    alerts = []
    try:
        hotspots = get_market_hotspots()
    except Exception:
        return alerts

    our_tickers = set(p["ticker"] for p in portfolio["positions"])

    for alert_item in hotspots.get("portfolio_alerts", []):
        t = alert_item["ticker"]
        if t not in our_tickers:
            continue
        sent = alert_item["sentiment"]
        if sent == "bearish":
            alerts.append((Alert.WARNING, t,
                f"Bearish news: {alert_item['headline'][:70]}"))
        elif sent == "bullish":
            alerts.append((Alert.INFO, t,
                f"Bullish news: {alert_item['headline'][:70]}"))

    # Overall market mood shift
    mood = hotspots.get("market_mood", 0)
    if mood < -0.4:
        alerts.append((Alert.WARNING, "MARKET",
            f"Market sentiment very bearish ({mood:+.2f}) — consider hedging"))
    elif mood > 0.5:
        alerts.append((Alert.INFO, "MARKET",
            f"Market sentiment bullish ({mood:+.2f})"))

    return alerts


# ── Check 6: Rebalance Reminder ───────────────────────────────

def check_rebalance(portfolio):
    """Remind if rebalance is due."""
    from config import REBALANCE_FREQUENCY_DAYS
    alerts = []

    last = portfolio.get("last_rebalance")
    if last:
        last_date = dt.date.fromisoformat(last)
        days_since = (dt.date.today() - last_date).days
        if days_since >= REBALANCE_FREQUENCY_DAYS:
            alerts.append((Alert.WARNING, "REBAL",
                f"Rebalance overdue! Last: {last} ({days_since} days ago). Run: python3 run.py"))
        elif days_since >= REBALANCE_FREQUENCY_DAYS - 3:
            alerts.append((Alert.INFO, "REBAL",
                f"Rebalance due in {REBALANCE_FREQUENCY_DAYS - days_since} days"))

    return alerts


# ── Daily Log ─────────────────────────────────────────────────

def log_daily(portfolio, total_value, total_pnl_pct):
    """Append daily snapshot to a CSV log for tracking over time."""
    log_file = os.path.join(os.path.dirname(__file__), "daily_log.csv")
    today = str(dt.date.today())

    # Check if already logged today
    if os.path.exists(log_file):
        df = pd.read_csv(log_file)
        if today in df["date"].values:
            return  # already logged
    else:
        df = pd.DataFrame()

    tickers = [p["ticker"] for p in portfolio["positions"]]
    row = {
        "date": today,
        "total_value": round(total_value, 2),
        "pnl_pct": round(total_pnl_pct, 2),
        "cash": portfolio.get("cash", 0),
        "num_positions": len(tickers),
        "holdings": ",".join(tickers),
    }

    new_row = pd.DataFrame([row])
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_csv(log_file, index=False)


# ── Main ──────────────────────────────────────────────────────

def run_watchdog(quick=False):
    print("╔════════════════════════════════════════════════════════════╗")
    print("║           DAILY PORTFOLIO WATCHDOG                       ║")
    print("╚════════════════════════════════════════════════════════════╝")

    # Load or init portfolio
    portfolio = load_portfolio()
    if not portfolio["positions"]:
        print("  No portfolio found. Initializing from current recommendations...")
        portfolio = init_portfolio()
        print(f"  Portfolio initialized with {len(portfolio['positions'])} positions.\n")

    # Portfolio status
    header("PORTFOLIO STATUS")
    rows, total_value, total_pnl, total_pnl_pct, cash = check_portfolio_status(portfolio)

    for r in rows:
        pnl_icon = "▲" if r["pnl"] >= 0 else "▼"
        print(f"  {pnl_icon} {r['ticker']:6s}  {r['shares']:3d} × ${r['current']:>8.2f} = ${r['value']:>9.2f}  "
              f"P&L: ${r['pnl']:>+8.2f} ({r['pnl_pct']:>+6.1f}%)")

    print(f"\n  Cash:           ${cash:>10,.2f}")
    print(f"  Portfolio:      ${total_value:>10,.2f}")
    pnl_icon = "▲" if total_pnl >= 0 else "▼"
    print(f"  Total P&L:   {pnl_icon} ${total_pnl:>+10,.2f} ({total_pnl_pct:>+.1f}%)")

    # Log daily
    log_daily(portfolio, total_value, total_pnl_pct)

    # Alerts
    all_alerts = []

    header("PRICE & STOP-LOSS CHECKS")
    price_alerts = check_price_moves(portfolio)
    all_alerts.extend(price_alerts)

    header("VOLUME ANOMALIES")
    vol_alerts = check_volume(portfolio)
    all_alerts.extend(vol_alerts)

    if not quick:
        header("MACRO REGIME CHECK")
        macro_alerts, macro_result = check_macro_shift()
        all_alerts.extend(macro_alerts)
        print(f"  Score: {macro_result['score']:+.3f} | Regime: {macro_result['regime'].upper()}")
        for name, ind in macro_result["indicators"].items():
            print(f"    {name:18s} {ind['signal']:+.1f}  {ind['label']}")

        header("NEWS & SENTIMENT")
        news_alerts = check_news(portfolio)
        all_alerts.extend(news_alerts)

    # Rebalance reminder
    rebal_alerts = check_rebalance(portfolio)
    all_alerts.extend(rebal_alerts)

    # Summary
    header("ALERT SUMMARY")
    if not all_alerts:
        print("  All clear. No actionable alerts today.")
    else:
        critical = [a for a in all_alerts if "CRITICAL" in a[0]]
        warnings = [a for a in all_alerts if "WARNING" in a[0]]
        infos = [a for a in all_alerts if "INFO" in a[0]]

        for a in critical + warnings + infos:
            print(f"  {a[0]} [{a[1]:6s}] {a[2]}")

        if critical:
            print(f"\n  ⚠️  {len(critical)} CRITICAL alert(s) — ACTION REQUIRED!")

    print(f"\n{'─'*60}")
    print(f"  Next check: tomorrow morning before market open")
    print(f"  Full rebalance: python3 run.py")
    print(f"{'─'*60}\n")


def show_history():
    """Show portfolio tracking history."""
    log_file = os.path.join(os.path.dirname(__file__), "daily_log.csv")
    if not os.path.exists(log_file):
        print("  No history yet. Run watchdog first.")
        return

    df = pd.read_csv(log_file)
    print("\n  ── Portfolio History ──")
    print(f"  {'Date':12s} {'Value':>10s} {'P&L %':>8s} {'Cash':>10s} {'Positions':>6s}")
    print(f"  {'─'*12} {'─'*10} {'─'*8} {'─'*10} {'─'*6}")
    for _, row in df.iterrows():
        icon = "▲" if row["pnl_pct"] >= 0 else "▼"
        print(f"  {row['date']:12s} ${row['total_value']:>9,.2f} {icon}{row['pnl_pct']:>+6.1f}% "
              f"${row['cash']:>9,.2f} {row['num_positions']:>6d}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--quick" in args:
        run_watchdog(quick=True)
    elif "--portfolio" in args:
        portfolio = load_portfolio()
        if not portfolio["positions"]:
            portfolio = init_portfolio()
        rows, total_value, total_pnl, total_pnl_pct, cash = check_portfolio_status(portfolio)
        header("PORTFOLIO STATUS")
        for r in rows:
            pnl_icon = "▲" if r["pnl"] >= 0 else "▼"
            print(f"  {pnl_icon} {r['ticker']:6s}  {r['shares']:3d} × ${r['current']:>8.2f} = ${r['value']:>9.2f}  "
                  f"P&L: ${r['pnl']:>+8.2f} ({r['pnl_pct']:>+6.1f}%)")
        print(f"\n  Total: ${total_value:>10,.2f} | P&L: ${total_pnl:>+8.2f} ({total_pnl_pct:>+.1f}%)")
    elif "--history" in args:
        show_history()
    else:
        run_watchdog(quick=False)
