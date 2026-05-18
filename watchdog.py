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

from broker import Broker
import orders
import config

# ── Portfolio state (via orders.sync_state) ─────────────────────


def snapshot() -> orders.PortfolioSnapshot:
    """Pull a fresh PortfolioSnapshot from Alpaca (source of truth).
    Alerts about unknown positions and missing brackets are returned separately.
    """
    broker = Broker(env=config.ALPACA_ENV)
    alerts: list = []
    snap = orders.sync_state(broker, alerts=alerts)
    snapshot.last_alerts = alerts  # type: ignore[attr-defined]
    return snap


snapshot.last_alerts = []  # type: ignore[attr-defined]


def _as_legacy_positions(snap: orders.PortfolioSnapshot) -> list[dict]:
    """Map snapshot position dicts to the legacy fields the check_* functions expect."""
    return [
        {
            "ticker": p["symbol"],
            "shares": p["shares"],
            "entry_price": p["avg_entry"],
            "entry_date": "",
            "tranche": p.get("tranche", "core"),
        }
        for p in snap.positions
        if p.get("tranche") != "unknown"
    ]


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


# ── SEPA take-profit (Phase 1) ───────────────────────────────────

def _sepa_notify(message: str, lines: list) -> None:
    """Append a Telegram message; also push to the in-process `lines` list
    so the caller can include them in the watchdog alert summary."""
    lines.append(message)
    path = getattr(config, "TELEGRAM_NOTIFY_PATH", None)
    if not path:
        return
    import json as _json
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    existing = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = _json.load(f)
        except Exception:
            existing = []
    existing.append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "watchdog.sepa",
        "message": message,
    })
    with open(path, "w") as f:
        _json.dump(existing, f, indent=2, default=str)


def _cancel_pending_partials(symbol: str) -> None:
    """Drop SEPA-side sell intents on `symbol` from .cache/pending_plan.json.

    Filters to (side=="sell" AND reason.startswith("sepa-")) so we never
    affect rebalance buys or non-SEPA exits. Idempotent: no plan or no
    matching intents → no-op.
    """
    from pending_plan import load_plan, write_plan
    plan = load_plan()
    if plan is None:
        return
    keep = [
        s for s in plan.intents
        if not (s.intent.symbol == symbol
                and s.intent.side == "sell"
                and s.intent.reason.startswith("sepa-"))
    ]
    if len(keep) == len(plan.intents):
        return
    plan.intents = keep
    write_plan(plan)


def _set_climax_fired(symbol: str) -> None:
    """Set climax_fired=True for `symbol` in the portfolio cache."""
    import json as _json
    cache = orders._load_portfolio_cache()
    for p in cache.get("positions", []):
        if p["symbol"] == symbol:
            p["climax_fired"] = True
            break
    with open(orders.PORTFOLIO_PATH, "w") as f:
        _json.dump(cache, f, indent=2, default=str)


def check_sepa_exits(snap: "orders.PortfolioSnapshot", broker) -> list:
    """SEPA Phase 1 driver. Returns notification lines for the alert summary.

    Per core position, in order:
      1. If next R-tier reached → submit_partial_exit (1/3 of initial_qty),
         cancel_position_trailing, re-trail at the remaining qty (unless this
         is the final tier — in which case no re-trail).
      2. If r_tier_filled contains the final tier label → check 21EMA;
         if close < EMA, submit_exit (full).
    """
    notifications: list = []
    if not getattr(config, "SEPA_ENABLED", False):
        return notifications

    import sepa_exits
    import data

    for pos in snap.by_tranche("core"):
        symbol = pos["symbol"]
        if pos.get("initial_stop_price") is None:
            continue
        try:
            current_price = float(broker._latest_price(symbol))
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: no latest price ({e})")
            continue

        # Phase 2 — 1. Failed-breakout (highest priority)
        try:
            ohlcv = data.fetch_ohlcv([symbol], period=config.SEPA_MA_HISTORY)
            close_series = (ohlcv["Close"][symbol]
                            if symbol in ohlcv["Close"].columns
                            else ohlcv["Close"].iloc[:, 0]).dropna()
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed: {e}")
            continue

        pivots = orders._load_entry_pivots()
        today_date = dt.datetime.now(dt.timezone.utc).date()
        if sepa_exits.failed_breakout(
            pos, pivots, close_series,
            today=today_date,
            window_days=config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS,
        ):
            _cancel_pending_partials(symbol)
            orders.cancel_position_trailing(symbol, broker=broker)
            orders.submit_exit(symbol, reason="sepa-failed-breakout", broker=broker)
            pivot_price = float(pivots[symbol]["pivot"])
            _sepa_notify(
                f"⚠ SEPA failed-breakout — {symbol}\n"
                f"Recent close ${float(close_series.iloc[-1]):.2f} < entry pivot "
                f"${pivot_price:.2f}; full exit triggered.",
                notifications,
            )
            continue

        # Phase 2 — 2. Climax (only if not already fired)
        if not pos.get("climax_fired"):
            if sepa_exits.climax_check(
                ohlcv,
                return_lookback=config.SEPA_CLIMAX_RETURN_LOOKBACK,
                return_threshold=config.SEPA_CLIMAX_RETURN_THRESHOLD,
                range_lookback=config.SEPA_CLIMAX_RANGE_LOOKBACK,
                range_multiplier=config.SEPA_CLIMAX_RANGE_MULTIPLIER,
                volume_lookback=config.SEPA_CLIMAX_VOLUME_LOOKBACK,
                volume_multiplier=config.SEPA_CLIMAX_VOLUME_MULTIPLIER,
                volume_recent_days=config.SEPA_CLIMAX_VOLUME_RECENT_DAYS,
            ):
                _cancel_pending_partials(symbol)
                orders.cancel_position_trailing(symbol, broker=broker)

                # Sell 50% of CURRENT remaining market value, directly via execute_plan
                # (no slicing — climax is "sell into strength, get out today").
                half_mv = float(pos["market_value"]) * 0.5
                cid = orders._make_cid("core", "sepa-climax", symbol, today_date)
                sell_intent = orders.OrderIntent(
                    symbol=symbol, notional=round(half_mv, 2), side="sell",
                    reason="sepa-climax", tranche="core", client_order_id=cid,
                )
                orders.execute_plan(
                    orders.OrderPlan(buys=[], sells=[sell_intent], holds=[]),
                    broker=broker, reason="sepa-climax",
                )

                # Tighter trailing on the (estimated) remaining qty.
                remaining_qty = float(pos["shares"]) * 0.5
                trail_cid = orders._make_cid("core", "climax-trail", symbol, today_date)
                try:
                    broker.submit_trailing_stop(
                        symbol, qty=remaining_qty,
                        trail_percent=config.SEPA_CLIMAX_TRAIL_PCT,
                        client_order_id=trail_cid,
                    )
                except Exception as e:
                    notifications.append(f"⚠ SEPA {symbol}: climax re-trail failed: {e}")

                _set_climax_fired(symbol)
                _sepa_notify(
                    f"🔥 SEPA climax — {symbol}\n"
                    f"Triple condition met; sold ~50% (~${half_mv:,.0f}) at "
                    f"${current_price:.2f}; trailing tightened to "
                    f"{config.SEPA_CLIMAX_TRAIL_PCT*100:.0f}%; "
                    f"R-multiple scale-out disabled.",
                    notifications,
                )
                continue

        # Phase 1 — 3. R-multiple scale-out (gated by !climax_fired in Phase 2)
        if not pos.get("climax_fired"):
            action = sepa_exits.next_r_tier_action(pos, current_price)
            if action is not None:
                # Fraction = the fraction associated with this tier label in SEPA_R_TIERS.
                frac = next(
                    (f for (r, f) in config.SEPA_R_TIERS if f"{int(r)}R" == action),
                    None,
                )
                if frac is None:
                    continue

                partial_result = orders.submit_partial_exit(
                    symbol, fraction_of_initial=frac,
                    reason=f"sepa-{action}", broker=broker,
                )
                orders.cancel_position_trailing(symbol, broker=broker)

                # Re-trail unless this is the final tier label.
                final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
                if action != final_label:
                    remaining_fraction = 1.0 - sum(
                        f for (r, f) in config.SEPA_R_TIERS
                        if f"{int(r)}R" in pos.get("r_tier_filled", []) or f"{int(r)}R" == action
                    )
                    new_qty = float(pos["initial_qty"]) * remaining_fraction
                    from orders import _make_cid
                    _, trail_pct = orders._tranche_stops("core")
                    cid = _make_cid("core", f"sepa-trail-{action}", symbol, dt.date.today())
                    try:
                        broker.submit_trailing_stop(symbol, qty=new_qty,
                                                    trail_percent=trail_pct,
                                                    client_order_id=cid)
                    except Exception as e:
                        notifications.append(f"⚠ SEPA {symbol}: re-trail failed: {e}")

                sold_dollars = float(pos["initial_qty"]) * frac * current_price
                sold_shares = float(pos["initial_qty"]) * frac
                tail_msg = (" — trailing-stop removed, now MA-trailing"
                            if action == final_label else "")
                _sepa_notify(
                    f"🎯 SEPA {action} hit — {symbol}\n"
                    f"Sold ~{sold_shares:.2f} shares ≈ ${sold_dollars:,.0f} at ${current_price:.2f}"
                    f"{tail_msg}",
                    notifications,
                )
                continue  # Don't also check MA on the same run; next watchdog observes qty drop.

        # Phase 1+2 — 4. 21EMA trail
        # Original gate: final R-tier filled. Phase 2 extends: also active
        # after climax_fired so the remaining 50% has an MA backstop.
        final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
        if (final_label not in (pos.get("r_tier_filled") or [])
                and not pos.get("climax_fired")):
            continue
        try:
            prices = data.fetch_prices([symbol], period=config.SEPA_MA_HISTORY)
            closes = (prices[symbol] if symbol in prices.columns
                      else prices.iloc[:, 0]).dropna()
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed: {e}")
            continue
        if sepa_exits.ma_trail_should_exit(pos, closes):
            orders.submit_exit(symbol, reason="sepa-21EMA-break", broker=broker)
            _sepa_notify(
                f"📉 SEPA 21EMA break — {symbol}\n"
                f"Last close ${float(closes.iloc[-1]):.2f} below 21EMA; "
                f"exiting remaining shares.",
                notifications,
            )

    # Phase 2 — end-of-pass GC: drop pivot records for symbols no longer held.
    held_symbols = {p["symbol"] for p in snap.by_tranche("core")}
    pivots_all = orders._load_entry_pivots()
    pruned = {k: v for k, v in pivots_all.items() if k in held_symbols}
    if len(pruned) != len(pivots_all):
        orders._save_entry_pivots(pruned)

    return notifications


# ── Macro-flip action ─────────────────────────────────────────

def act_on_macro_flip(snap: orders.PortfolioSnapshot, regime: str) -> list:
    """If macro regime turned bearish today, exit leveraged-ETF aggressive positions."""
    if regime != "contraction":
        return []

    broker = Broker(env=config.ALPACA_ENV)
    notifications: list = []
    for p in snap.by_tranche("aggressive"):
        sym = p["symbol"]
        if sym not in config._ETF_LEVERAGED:
            continue
        result = orders.submit_exit(sym, reason="macro-contraction", broker=broker)
        if result.submitted:
            notifications.append(f"Exited {sym} on macro flip.")
        for _, msg in result.skipped:
            notifications.append(f"Could not exit {sym}: {msg}")
    return notifications


# ── Check 4: Macro Shifts ─────────────────────────────────────

def check_macro_shift(snap=None):
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

    # Auto-exit leveraged ETFs on contraction
    notifications = act_on_macro_flip(snap if snap is not None else snapshot(), regime)
    for n in notifications:
        alerts.append((Alert.CRITICAL, "MACRO", n))

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

    # Load portfolio state from Alpaca
    snap = snapshot()
    portfolio = {
        "positions": _as_legacy_positions(snap),
        "cash": snap.cash,
        "initial_capital": config.INITIAL_CAPITAL,
    }

    # Safety net: attach trailing stops to any known-tranche position missing one.
    # Rebalancer normally does this at submit time; this catches anything that
    # slipped through (e.g., buy filled after rebalancer exited, or bracket
    # attach failed).
    broker = Broker(env=config.ALPACA_ENV)
    trail_result = orders.ensure_trailing_stops(broker)
    if trail_result.submitted:
        print(f"  Attached {len(trail_result.submitted)} missing trailing stop(s):")
        for o in trail_result.submitted:
            print(f"    • {o.symbol}")
    if trail_result.skipped:
        for pair in trail_result.skipped:
            sym = pair[0].symbol if pair[0] is not None else "?"
            print(f"    ! Could not attach trailing stop on {sym}: {pair[1]}")

    # SEPA Phase 1 take-profit checks (R-multiple scale-out + 21EMA trail)
    header("SEPA EXITS")
    sepa_lines = check_sepa_exits(snap, broker)
    if not sepa_lines:
        print("  No SEPA actions today.")
    else:
        for line in sepa_lines:
            print(f"  {line}")

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
        macro_alerts, macro_result = check_macro_shift(snap)
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
        snap = snapshot()
        portfolio = {
            "positions": _as_legacy_positions(snap),
            "cash": snap.cash,
            "initial_capital": config.INITIAL_CAPITAL,
        }
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
