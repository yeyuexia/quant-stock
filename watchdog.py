#!/usr/bin/env python3
"""
Portfolio Watchdog

Usage:
  python3 watchdog.py              # full daily check
  python3 watchdog.py --quick      # price + stop-loss only
  python3 watchdog.py --intraday   # lightweight intraday check (SEPA + stop-loss)
  python3 watchdog.py --portfolio  # show current portfolio status

Cron:
  # Full daily check at 8:30 AM ET (before market open):
  30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1

  # Intraday check every 5 min during market hours (9:30–16:00 ET):
  */5 9-16 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py --intraday >> .cache/watchdog_intraday.log 2>&1
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


_DEGRADED_SENTINEL_PATH = os.path.join(os.path.dirname(__file__), ".cache",
                                       "snapshot_degraded_since.json")


def _enter_degraded(reason: str) -> bool:
    """Record degraded entry. Returns True iff this is a state TRANSITION
    (healthy → degraded) so the caller only notifies on first occurrence."""
    if os.path.exists(_DEGRADED_SENTINEL_PATH):
        return False  # already degraded — no transition, no spam
    os.makedirs(os.path.dirname(_DEGRADED_SENTINEL_PATH), exist_ok=True)
    with open(_DEGRADED_SENTINEL_PATH, "w") as f:
        json.dump({
            "since": dt.datetime.now(dt.timezone.utc).isoformat(),
            "reason": reason,
        }, f)
    return True


def _exit_degraded() -> "tuple[bool, float]":
    """Clear degraded state. Returns (was_degraded, minutes_in_degraded)."""
    if not os.path.exists(_DEGRADED_SENTINEL_PATH):
        return (False, 0.0)
    try:
        with open(_DEGRADED_SENTINEL_PATH) as f:
            data = json.load(f)
        since = dt.datetime.fromisoformat(data["since"])
        minutes = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 60
    except (json.JSONDecodeError, KeyError, ValueError):
        minutes = 0.0
    try:
        os.remove(_DEGRADED_SENTINEL_PATH)
    except OSError:
        pass
    return (True, minutes)


def snapshot(broker=None) -> orders.PortfolioSnapshot:
    """Pull a fresh PortfolioSnapshot from Alpaca (source of truth).

    Tolerant to transient broker failures: retries once, then falls back to
    the on-disk portfolio.json cache (last successful sync). TG notifications
    only fire on state TRANSITIONS (healthy → degraded, degraded → recovered)
    — not every tick we're degraded, to avoid spamming the bot during outages.
    """
    if broker is None:
        broker = Broker(env=config.ALPACA_ENV)
    alerts: list = []

    last_exc = None
    for attempt in range(2):
        try:
            snap = orders.sync_state(broker, alerts=alerts)
            # Healthy path: if we were in degraded state, notify recovery.
            was_degraded, minutes_down = _exit_degraded()
            if was_degraded:
                try:
                    from notifications import append_notification
                    append_notification({
                        "source": "watchdog.snapshot",
                        "message": (f"✓ snapshot RECOVERED after "
                                    f"{minutes_down:.0f} minutes in degraded mode."),
                    })
                except Exception:
                    pass
            snapshot.last_alerts = alerts  # type: ignore[attr-defined]
            return snap
        except Exception as e:
            last_exc = e
            if attempt == 0:
                import time as _time
                _time.sleep(0.5)

    # Both attempts failed — fall back to the cache so we can still run SEPA
    # exits, stop-loss, macro etc. against the last known state.
    reason = f"{type(last_exc).__name__}: {last_exc}"
    if _enter_degraded(reason):
        # State transition: healthy → degraded. One notification only.
        try:
            from notifications import append_notification
            append_notification({
                "source": "watchdog.snapshot",
                "message": (f"⚠ CRITICAL: snapshot DEGRADED to portfolio.json "
                            f"cache ({reason}). Subsequent degraded ticks will "
                            f"NOT re-notify until recovery."),
            })
        except Exception:
            pass

    cache = orders._load_portfolio_cache() or {}
    snap = orders.PortfolioSnapshot(
        synced_at=cache.get("synced_at", ""),
        alpaca_env=cache.get("alpaca_env", config.ALPACA_ENV),
        cash=float(cache.get("cash", 0.0) or 0.0),
        equity=float(cache.get("equity", 0.0) or 0.0),
        positions=cache.get("positions", []),
        tranches=cache.get("tranches", {}),
    )
    snapshot.last_alerts = [  # type: ignore[attr-defined]
        f"DEGRADED: using cached portfolio.json ({reason})",
    ]
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
            # unknown tranche (externally-bought) falls back to core stop-loss rules
            "tranche": "core" if p.get("tranche") == "unknown" else p.get("tranche", "core"),
        }
        for p in snap.positions
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

def check_price_moves(portfolio, broker=None):
    """Check for significant price moves in holdings.

    Uses 6-month price history so "peak since entry" is genuinely the high
    since the position was opened (previous version used a 5-day window which
    silently misrepresented the trailing-stop reference for any position held
    longer than a week).

    When *broker* is supplied, current price is fetched via broker._latest_price
    (Alpaca real-time / IEX feed) so intraday triggers don't wait for the
    daily close. prev_close + peak still come from yfinance daily bars.
    """
    from data import fetch_prices, fetch_info

    alerts = []
    tickers = [p["ticker"] for p in portfolio["positions"]]
    if not tickers:
        return alerts

    # 6mo so even positions held for months have a meaningful peak. yfinance
    # cache makes the additional history nearly free after the first warm-up.
    prices = fetch_prices(tickers, period="6mo")

    for pos in portfolio["positions"]:
        t = pos["ticker"]
        if t not in prices.columns:
            continue

        s = prices[t].dropna()
        if len(s) < 2:
            continue

        if broker is not None:
            try:
                current = float(broker._latest_price(t))
            except Exception:
                current = s.iloc[-1]
        else:
            current = s.iloc[-1]
        prev_close = s.iloc[-2]
        entry = pos["entry_price"]

        # Daily change
        daily_chg = (current / prev_close - 1) * 100

        # Change from entry
        from_entry = (current / entry - 1) * 100

        # Peak since entry. Slice the series at entry_date when we know it,
        # else use the entry price as a floor so pre-entry highs don't count.
        entry_date_str = pos.get("entry_date") or ""
        try:
            entry_date = dt.date.fromisoformat(entry_date_str) if entry_date_str else None
        except (TypeError, ValueError):
            entry_date = None

        if entry_date is not None:
            after_entry = s[s.index.date >= entry_date]
            peak = float(after_entry.max()) if not after_entry.empty else float(s.max())
        else:
            # Unknown entry date — bound peak below by entry price so a higher
            # pre-entry print doesn't inflate the "from peak" warning level.
            peak = float(max(s.max(), entry))
        # current too — the trailing-stop reference can only be ≥ current.
        peak = max(peak, float(current))
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
    """Calculate current portfolio value and P&L.

    One batched fetch_prices call for all holdings (taking the last close as
    'current'). The old per-ticker fetch_info loop spent ~0.5-1s per ticker on
    Ticker.info — wasteful for daily runs with 4-10 positions.
    """
    from data import fetch_prices

    rows = []
    total_value = 0
    total_cost = 0

    tickers = [p["ticker"] for p in portfolio["positions"]]
    current_by_ticker: dict = {}
    if tickers:
        try:
            prices = fetch_prices(tickers, period="5d")
        except Exception:
            prices = None
        if prices is not None and not prices.empty:
            for t in tickers:
                if t in prices.columns:
                    series = prices[t].dropna()
                    if not series.empty:
                        current_by_ticker[t] = float(series.iloc[-1])

    for pos in portfolio["positions"]:
        t = pos["ticker"]
        shares = pos["shares"]
        entry = pos["entry_price"]
        cost = shares * entry

        current = current_by_ticker.get(t, entry)

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
    """Check for unusual volume (>2x 20-day average).

    One batched yfinance call for all holdings instead of N serial calls
    (the old per-ticker loop is the same anti-pattern we batched out of
    check_sepa_exits / check_buy_signals — same fix here for the daily path).
    """
    import yfinance as yf

    alerts = []
    tickers = [p["ticker"] for p in portfolio["positions"]]
    if not tickers:
        return alerts

    try:
        all_data = yf.download(
            tickers, period="1mo", progress=False, group_by="ticker",
        )
    except Exception as exc:
        alerts.append((Alert.WARNING, "VOL", f"batch yfinance failed: {exc}"))
        return alerts

    if all_data is None or all_data.empty:
        return alerts

    def _vol_series(ticker):
        if isinstance(all_data.columns, pd.MultiIndex):
            try:
                return all_data[ticker]["Volume"].dropna()
            except (KeyError, IndexError):
                return None
        # Single-ticker case (yfinance flattens)
        if len(tickers) == 1:
            return all_data["Volume"].dropna()
        return None

    for t in tickers:
        vol = _vol_series(t)
        if vol is None or len(vol) < 5:
            continue
        avg_vol = vol.iloc[:-1].mean()
        last_vol = vol.iloc[-1]
        if avg_vol > 0 and last_vol > avg_vol * 2:
            ratio = last_vol / avg_vol
            alerts.append((Alert.WARNING, t,
                f"Volume spike: {ratio:.1f}x avg "
                f"({last_vol/1e6:.1f}M vs {avg_vol/1e6:.1f}M avg)"))

    return alerts


# ── SEPA take-profit (Phase 1) ───────────────────────────────────

def _sepa_notify(message: str, lines: list) -> None:
    """Append a Telegram message; also push to the in-process `lines` list
    so the caller can include them in the watchdog alert summary."""
    lines.append(message)
    from notifications import append_notification
    append_notification({"source": "watchdog.sepa", "message": message})


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
    """Set climax_fired=True for `symbol` in the portfolio cache.

    Uses a .lock sidecar (codebase convention) instead of locking the data
    file directly — keeps the lock target separate from the JSON we mutate.
    """
    import fcntl, json as _json
    path = orders.PORTFOLIO_PATH
    if not os.path.exists(path):
        return  # sync_state hasn't run yet; nothing to mark
    lock_path = path + ".lock"
    with open(lock_path, "w") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        with open(path, "r") as f:
            try:
                cache = _json.load(f)
            except (_json.JSONDecodeError, ValueError):
                cache = {}
        for p in cache.get("positions", []):
            if p["symbol"] == symbol:
                p["climax_fired"] = True
                break
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            _json.dump(cache, f, indent=2, default=str)
        os.replace(tmp_path, path)


def check_sepa_exits(snap: "orders.PortfolioSnapshot", broker,
                     *, live_prices: bool = False) -> list:
    """SEPA Phase 1 driver. Returns notification lines for the alert summary.

    Per core position, in order:
      1. If next R-tier reached → submit_partial_exit (1/3 of initial_qty),
         cancel_position_trailing, re-trail at the remaining qty (unless this
         is the final tier — in which case no re-trail).
      2. If r_tier_filled contains the final tier label → check 21EMA;
         if close < EMA, submit_exit (full).

    Performance: this function used to issue (N × broker._latest_price) +
    (N × data.fetch_ohlcv) round-trips, fired every 5 min by the intraday
    cron — ≈ 300+ HTTP calls per day for 4 positions. We now batch both:
      - data.fetch_ohlcv(all_core_symbols, ...) once at the top
      - current price either from snap.market_value/shares (default, free,
        possibly 0-5min stale) OR from a single batched broker.latest_quote
        loop when `live_prices=True` (intraday mode wants real-time so fast
        R-tier triggers don't fire ~5 min late).
    """
    notifications: list = []
    if not getattr(config, "SEPA_ENABLED", False):
        return notifications

    import sepa_exits
    import data

    core_positions = [
        p for p in snap.positions
        if p.get("tranche") in ("core", "unknown")
        and p.get("initial_stop_price") is not None
    ]
    if not core_positions:
        return notifications

    core_symbols = [p["symbol"] for p in core_positions]

    # When live_prices=True, batch-quote all core symbols up front instead of
    # per-position broker calls inside the loop. Falls back to snap-derived
    # price when a quote fetch fails (network blip on one symbol shouldn't
    # block the others).
    live_price_by_symbol: dict = {}
    if live_prices:
        for sym in core_symbols:
            try:
                bid, ask = broker.latest_quote(sym)
                live_price_by_symbol[sym] = (bid + ask) / 2
            except Exception:
                continue

    # ── Batch prefetch: one yfinance call for all core OHLCV ──────
    try:
        batched_ohlcv = data.fetch_ohlcv(core_symbols, period=config.SEPA_MA_HISTORY)
    except Exception as e:
        notifications.append(f"⚠ SEPA: batched OHLCV fetch failed: {e} — "
                             f"falling back to per-symbol fetch")
        batched_ohlcv = None

    def _close_series_for(symbol):
        """Pull a single ticker's Close series out of the batched frame, with
        per-symbol fallback if the batch fetch failed or this symbol is missing."""
        if batched_ohlcv is not None:
            try:
                col = (batched_ohlcv["Close"][symbol]
                       if symbol in batched_ohlcv["Close"].columns
                       else batched_ohlcv["Close"].iloc[:, 0])
                return col.dropna()
            except (KeyError, IndexError):
                pass
        # Fallback: per-symbol fetch
        try:
            ohlcv = data.fetch_ohlcv([symbol], period=config.SEPA_MA_HISTORY)
            return (ohlcv["Close"][symbol]
                    if symbol in ohlcv["Close"].columns
                    else ohlcv["Close"].iloc[:, 0]).dropna()
        except Exception:
            return None

    def _ohlcv_for(symbol):
        """Per-symbol OHLCV slice for climax_check (which needs O/H/L/C/V).
        Tries to slice the batched frame; falls back to per-symbol fetch."""
        if batched_ohlcv is not None:
            try:
                fields = {}
                for col in ("Open", "High", "Low", "Close", "Volume"):
                    if col not in batched_ohlcv.columns.levels[0]:
                        continue
                    sub = batched_ohlcv[col]
                    if symbol in sub.columns:
                        fields[(col, symbol)] = sub[symbol]
                if fields:
                    out = pd.DataFrame(fields, index=batched_ohlcv.index)
                    out.columns = pd.MultiIndex.from_tuples(out.columns)
                    return out
            except (KeyError, IndexError, AttributeError):
                pass
        try:
            return data.fetch_ohlcv([symbol], period=config.SEPA_MA_HISTORY)
        except Exception:
            return None

    for pos in core_positions:
        symbol = pos["symbol"]

        shares = float(pos.get("shares") or 0)
        if shares == 0:
            notifications.append(f"⚠ SEPA {symbol}: zero shares, skipping")
            continue

        # Prefer the live quote when intraday explicitly asked for real-time;
        # otherwise derive from snap (no network call, possibly slightly stale).
        if symbol in live_price_by_symbol:
            current_price = float(live_price_by_symbol[symbol])
        else:
            current_price = float(pos["market_value"]) / shares

        close_series = _close_series_for(symbol)
        if close_series is None or close_series.empty:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed")
            continue

        ohlcv = _ohlcv_for(symbol)
        if ohlcv is None:
            notifications.append(f"⚠ SEPA {symbol}: ohlcv fetch failed")
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
            orders.submit_exit(symbol, reason="sepa-failed-breakout",
                               broker=broker, current_price=current_price)
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
            # symbol is now explicit — climax_check used to pick alphabetically-
            # first column from ohlcv["Close"]; with batched fetches that could
            # silently look at the wrong ticker.
            if sepa_exits.climax_check(ohlcv, symbol):
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
                    current_price=current_price,
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
        # Reuse close_series from the batched OHLCV fetch — no extra HTTP.
        if sepa_exits.ma_trail_should_exit(pos, close_series):
            orders.submit_exit(symbol, reason="sepa-21EMA-break", broker=broker,
                               current_price=current_price)
            _sepa_notify(
                f"📉 SEPA 21EMA break — {symbol}\n"
                f"Last close ${float(close_series.iloc[-1]):.2f} below 21EMA; "
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

def act_on_macro_flip(snap: orders.PortfolioSnapshot, regime: str,
                       broker=None) -> list:
    """If macro regime turned bearish today, exit leveraged-ETF aggressive positions."""
    if regime != "contraction":
        return []

    if broker is None:
        broker = Broker(env=config.ALPACA_ENV)
    notifications: list = []
    for p in snap.by_tranche("aggressive"):
        sym = p["symbol"]
        if sym not in config.ETF_LEVERAGED:
            continue
        result = orders.submit_exit(sym, reason="macro-contraction", broker=broker)
        if result.submitted:
            notifications.append(f"Exited {sym} on macro flip.")
        for _, msg in result.skipped:
            notifications.append(f"Could not exit {sym}: {msg}")
    return notifications


# ── Check 4: Macro Shifts ─────────────────────────────────────

_MACRO_SCORE_PATH = os.path.join(os.path.dirname(__file__), ".cache",
                                 "last_macro_score.json")


def check_macro_shift(snap=None, broker=None):
    """Check if macro regime has changed since last check."""
    from macro import macro_regime_score

    alerts = []
    result = macro_regime_score()
    score = result["score"]
    regime = result["regime"]

    # Load previous score (module-level constant lets tests monkeypatch the path)
    score_file = _MACRO_SCORE_PATH
    prev_score = None
    prev_regime = None
    if os.path.exists(score_file):
        with open(score_file) as f:
            prev = json.load(f)
            prev_score = prev.get("score")
            prev_regime = prev.get("regime")

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

    # Auto-exit leveraged ETFs on contraction BEFORE persisting the new score.
    # Rationale: if act_on_macro_flip fails (broker down, cash gate, etc.) we
    # don't want prev_regime advanced to the new value — tomorrow's run must
    # re-detect the flip and re-attempt the exit. Score is only persisted when
    # the act half completed without raising.
    act_failed = False
    try:
        if snap is None:
            snap = snapshot(broker=broker)
        notifications = act_on_macro_flip(snap, regime, broker=broker)
    except Exception as e:
        act_failed = True
        notifications = [f"act_on_macro_flip raised {type(e).__name__}: {e}"]
    for n in notifications:
        alerts.append((Alert.CRITICAL, "MACRO", n))

    if not act_failed:
        os.makedirs(os.path.dirname(score_file), exist_ok=True)
        with open(score_file, "w") as f:
            json.dump({"score": score, "regime": regime, "date": str(dt.date.today())}, f)

    return alerts, result


# ── Check 5: News Alerts ──────────────────────────────────────

def check_news(portfolio):
    """Check for breaking news on our holdings."""
    from sentiment import get_market_hotspots

    alerts = []
    try:
        hotspots = get_market_hotspots()
    except Exception as e:
        # Don't silently disappear — surface the feed outage so we know to fix
        # it. One alert per run, not per ticker.
        msg = f"news/sentiment feed unavailable: {type(e).__name__}: {e}"
        alerts.append((Alert.WARNING, "NEWS", msg))
        try:
            _notify_critical(f"⚠ watchdog: {msg}")
        except Exception:
            pass
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

_REBALANCE_STALE_DAYS = 7   # cron rebalance fires daily; > 7 days means cron is broken

def check_rebalance(portfolio):
    """Health check: alert only when the daily rebalance cron appears stuck.

    Under daily cadence the old "rebalance due" reminder fires every single day
    and is pure noise. We instead surface ONE alert when last_rebalance is
    older than _REBALANCE_STALE_DAYS days (signals: cron not firing, broker
    persistently rejecting, or HALT file forgotten).
    """
    alerts = []
    last = portfolio.get("last_rebalance")
    if not last:
        return alerts
    try:
        last_date = dt.date.fromisoformat(last)
    except (TypeError, ValueError):
        return alerts
    days_since = (dt.date.today() - last_date).days
    if days_since >= _REBALANCE_STALE_DAYS:
        alerts.append((Alert.CRITICAL, "REBAL",
            f"No successful rebalance in {days_since} days (last: {last}). "
            f"Check cron / .cache/HALT / Alpaca connectivity."))
    return alerts


# ── Daily Log ─────────────────────────────────────────────────

_DAILY_LOG_COLUMNS = ["date", "total_value", "pnl_pct", "cash",
                       "num_positions", "holdings"]


def log_daily(portfolio, total_value, total_pnl_pct):
    """Append daily snapshot to a CSV log for tracking over time.

    Append-only: header written once on file creation, subsequent calls just
    append one CSV row. Avoids the quadratic read-concat-write cost of the
    old pandas-based path (which grew linearly with file size).
    Same-day dedup via a cheap last-line tail check.
    """
    import csv
    import fcntl
    log_file = os.path.join(os.path.dirname(__file__), "daily_log.csv")
    today = str(dt.date.today())
    lock_path = log_file + ".lock"

    with open(lock_path, "w") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)

        # Dedup: scan the last ~4KB for a row starting with today's date.
        # Cheap O(constant) instead of pandas reading the whole file.
        if os.path.exists(log_file):
            try:
                with open(log_file, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 4096))
                    tail = f.read().decode("utf-8", errors="replace")
                for line in reversed(tail.splitlines()):
                    if line.startswith(today + ","):
                        return  # already logged today
            except (OSError, UnicodeDecodeError):
                pass

        tickers = [p["ticker"] for p in portfolio["positions"]]
        row = [
            today,
            round(total_value, 2),
            round(total_pnl_pct, 2),
            portfolio.get("cash", 0),
            len(tickers),
            ",".join(tickers),
        ]

        write_header = not os.path.exists(log_file)
        with open(log_file, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(_DAILY_LOG_COLUMNS)
            w.writerow(row)


# ── Main ──────────────────────────────────────────────────────

def run_watchdog(quick=False):
    print("╔════════════════════════════════════════════════════════════╗")
    print("║           DAILY PORTFOLIO WATCHDOG                       ║")
    print("╚════════════════════════════════════════════════════════════╝")

    # Single Broker instance threaded through the rest of the run — saves
    # 2 extra Alpaca client constructions per invocation.
    broker = Broker(env=config.ALPACA_ENV)
    snap = snapshot(broker=broker)
    _last_rebalances = [
        v.get("last_rebalance")
        for v in snap.tranches.values()
        if isinstance(v, dict) and v.get("last_rebalance")
    ]
    portfolio = {
        "positions": _as_legacy_positions(snap),
        "cash": snap.cash,
        "initial_capital": config.INITIAL_CAPITAL,
        "last_rebalance": max(_last_rebalances) if _last_rebalances else None,
    }

    # Safety net: attach trailing stops to any known-tranche position missing one.
    # Rebalancer normally does this at submit time; this catches anything that
    # slipped through (e.g., buy filled after rebalancer exited, or bracket
    # attach failed).
    trail_result = orders.ensure_trailing_stops(broker)
    if trail_result.submitted:
        print(f"  Attached {len(trail_result.submitted)} missing trailing stop(s):")
        for o in trail_result.submitted:
            print(f"    • {o.symbol}")
    if trail_result.skipped:
        for pair in trail_result.skipped:
            sym = pair[0].symbol if pair[0] is not None else "?"
            print(f"    ! Could not attach trailing stop on {sym}: {pair[1]}")

    # SEPA exits run in run_intraday (every 5 min during RTH). The daily
    # 8:30 ET pass intentionally skips them to avoid duplicate evaluation —
    # intraday's first tick at 9:30 will run them anyway. Keep the header for
    # operational visibility.
    header("SEPA EXITS (skipped — handled by intraday cron)")

    # Portfolio status
    header("PORTFOLIO STATUS")
    rows, total_value, total_pnl, total_pnl_pct, cash = check_portfolio_status(portfolio)

    for r in rows:
        pnl_icon = "▲" if r["pnl"] >= 0 else "▼"
        print(f"  {pnl_icon} {r['ticker']:6s}  {r['shares']:>9.4f} × ${r['current']:>8.2f} = ${r['value']:>9.2f}  "
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
        macro_alerts, macro_result = check_macro_shift(snap, broker=broker)
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


# ── Intraday buy signals ─────────────────────────────────────────

_SCREENER_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".cache", "screener_result.json")


def _load_screener_cache() -> "pd.DataFrame | None":
    """Return cached screener DataFrame if younger than WATCHDOG_BUY_SCREENER_CACHE_HOURS."""
    if not os.path.exists(_SCREENER_CACHE_PATH):
        return None
    age_hours = (dt.datetime.now().timestamp() - os.path.getmtime(_SCREENER_CACHE_PATH)) / 3600
    if age_hours > config.WATCHDOG_BUY_SCREENER_CACHE_HOURS:
        return None
    try:
        import json as _json
        with open(_SCREENER_CACHE_PATH) as f:
            records = _json.load(f)
        return pd.DataFrame(records)
    except Exception:
        return None


def _save_screener_cache(df: "pd.DataFrame") -> None:
    """Write screener cache atomically: write-temp then rename, with fcntl
    lock on a sibling sentinel so a concurrent reader never sees partial JSON."""
    import fcntl, json as _json
    os.makedirs(os.path.dirname(_SCREENER_CACHE_PATH), exist_ok=True)
    lock_path = _SCREENER_CACHE_PATH + ".lock"
    with open(lock_path, "w") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        tmp_path = _SCREENER_CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            _json.dump(df.to_dict(orient="records"), f, default=str)
        os.replace(tmp_path, _SCREENER_CACHE_PATH)


def _get_screened_stocks() -> "pd.DataFrame":
    """Return screener results from 1-hour cache, or run a fresh screen."""
    cached = _load_screener_cache()
    if cached is not None and not cached.empty:
        return cached
    from screener import screen_stocks
    df = screen_stocks()
    if not df.empty:
        _save_screener_cache(df)
    return df


def _intraday_volume_fraction(minutes_elapsed: float) -> float:
    """Approximate fraction of full-day volume accumulated by `minutes_elapsed`
    minutes into the session.

    US equity intraday volume is U-shaped:
      • 9:30–10:30 (open burst):     ~25% of daily volume
      • 10:30–15:00 (midday trough): ~50%
      • 15:00–16:00 (closing burst): ~25%

    Returning the cumulative fraction lets the projector compute
    full_day = observed / fraction, which is dramatically more accurate
    at midday than a linear 1.0× projection.
    """
    if minutes_elapsed <= 0:
        return 0.0
    if minutes_elapsed <= 60:                      # 0–60 min into session
        return (minutes_elapsed / 60.0) * 0.25
    if minutes_elapsed <= 330:                     # 60–330 min (10:30–15:00)
        return 0.25 + ((minutes_elapsed - 60) / 270.0) * 0.50
    if minutes_elapsed <= 390:                     # 330–390 (15:00–16:00)
        return 0.75 + ((minutes_elapsed - 330) / 60.0) * 0.25
    return 1.0


def _estimate_full_day_volume(ticker: str,
                              bars: "pd.DataFrame | None" = None) -> "float | None":
    """Project today's full-day volume from intraday 1-min bars + a U-shape profile.

    Returns None when < WATCHDOG_BUY_MIN_ELAPSED_MIN into the session.
    `bars` lets callers pass a pre-fetched 1m frame (multi-ticker batch
    download) to avoid one yfinance round-trip per symbol — see check_buy_signals.
    """
    from timeutils import now_et
    now = now_et()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_elapsed = (now - market_open).total_seconds() / 60
    if minutes_elapsed < config.WATCHDOG_BUY_MIN_ELAPSED_MIN:
        return None

    if bars is None:
        import yfinance as yf
        bars = yf.download(ticker, period="1d", interval="1m",
                            progress=False, auto_adjust=True)
    if bars is None or bars.empty:
        return None

    if isinstance(bars.columns, pd.MultiIndex):
        try:
            current_vol = float(bars["Volume"][ticker].sum())
        except KeyError:
            return None
    else:
        current_vol = float(bars["Volume"].sum())

    fraction = _intraday_volume_fraction(minutes_elapsed)
    if fraction <= 0:
        return None
    return current_vol / fraction


_BUY_SIGNALS_TODAY_PATH = os.path.join(os.path.dirname(__file__),
                                        ".cache", "buy_signals_today.json")


def _load_today_buy_signals() -> set:
    """Tickers we've already fired buy signals for today. Resets at date change."""
    today = dt.date.today().isoformat()
    if not os.path.exists(_BUY_SIGNALS_TODAY_PATH):
        return set()
    try:
        with open(_BUY_SIGNALS_TODAY_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()
    if data.get("date") != today:
        return set()
    return set(data.get("tickers", []))


def _record_today_buy_signal(ticker: str) -> None:
    """Stamp `ticker` as having fired today. fcntl-safe."""
    import fcntl
    today = dt.date.today().isoformat()
    os.makedirs(os.path.dirname(_BUY_SIGNALS_TODAY_PATH), exist_ok=True)
    lock = _BUY_SIGNALS_TODAY_PATH + ".lock"
    with open(lock, "w") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        existing = _load_today_buy_signals()
        existing.add(ticker)
        with open(_BUY_SIGNALS_TODAY_PATH, "w") as f:
            json.dump({"date": today, "tickers": sorted(existing)}, f)


def check_buy_signals(snap: "orders.PortfolioSnapshot", broker: "Broker") -> list[str]:
    """Fire when a screened stock's estimated full-day volume exceeds the max
    volume on any down-day in the past WATCHDOG_BUY_LOOKBACK_DAYS trading days.
    Submits a buy order and writes a Telegram notification.

    Performance: batches all candidates into TWO yfinance calls per tick
    (history + intraday) instead of 2N. Critical because intraday cron fires
    every 5 min — the old per-ticker loop guaranteed yfinance throttling.

    Dedup: a ticker that already fired today (recorded in
    .cache/buy_signals_today.json) is skipped — avoids re-submitting the same
    buy 5 min later just because volume kept climbing.
    """
    import yfinance as yf

    screened = _get_screened_stocks()
    if screened.empty:
        return []

    owned = {p["symbol"] for p in snap.positions}
    fired_today = _load_today_buy_signals()
    candidates = [
        str(row["ticker"]) for _, row in screened.iterrows()
        if str(row["ticker"]) not in owned and str(row["ticker"]) not in fired_today
    ]
    if not candidates:
        return []

    lines: list[str] = []
    lookback = config.WATCHDOG_BUY_LOOKBACK_DAYS

    # ── Batch fetch: history (lookback+5 days) for ALL candidates in one call ──
    try:
        hist_all = yf.download(
            candidates, period=f"{lookback + 5}d",
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except Exception as e:
        lines.append(f"  [buy-check batch-history error]: {e}")
        return lines

    # ── Batch fetch: today's 1m bars for all candidates ──
    intraday_all = None
    try:
        intraday_all = yf.download(
            candidates, period="1d", interval="1m",
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except Exception as e:
        lines.append(f"  [buy-check batch-intraday error]: {e}")
        # We can still attempt per-ticker fallback inside the loop.

    def _per_ticker_frame(batched, ticker, multi_first: bool):
        """Slice batched yf.download output for one ticker. yfinance frame
        shape varies (single-ticker = flat; multi-ticker = MultiIndex). When
        we passed a list of tickers, group_by='ticker' makes ticker the top
        level."""
        if batched is None or batched.empty:
            return None
        if isinstance(batched.columns, pd.MultiIndex):
            try:
                if multi_first:
                    return batched[ticker]
                return batched.xs(ticker, axis=1, level=1)
            except (KeyError, ValueError):
                return None
        # Single ticker case (yfinance flattens when len(candidates)==1)
        return batched if len(candidates) == 1 else None

    for ticker in candidates:
        try:
            hist = _per_ticker_frame(hist_all, ticker, multi_first=True)
            if hist is None or hist.empty or len(hist) < 2:
                continue

            close = hist["Close"].dropna() if "Close" in hist.columns else None
            volume = hist["Volume"].dropna() if "Volume" in hist.columns else None
            if close is None or volume is None:
                continue

            df_h = pd.DataFrame({"close": close, "volume": volume}).dropna().iloc[:-1]
            df_h = df_h.tail(lookback)
            df_h = df_h.copy()
            df_h["prev_close"] = df_h["close"].shift(1)
            down_days = df_h[df_h["close"] < df_h["prev_close"]]
            if down_days.empty:
                continue

            max_down_vol = float(down_days["volume"].max())

            # Intraday bars come from the same batched frame when possible
            # (saves a yfinance call per candidate inside _estimate_full_day_volume).
            ticker_intraday = _per_ticker_frame(intraday_all, ticker, multi_first=True)
            est_vol = _estimate_full_day_volume(ticker, bars=ticker_intraday)
            if est_vol is None:
                continue

            if est_vol <= max_down_vol:
                continue

            # Buy signal fired — try to submit. Only stamp dedup if the order
            # actually went through; otherwise (HALT, cash-aware gate, etc.)
            # a later tick on the same day should still get a chance.
            msg = (f"BUY SIGNAL [{ticker}] est vol {est_vol/1e6:.1f}M "
                   f"> max down-day vol {max_down_vol/1e6:.1f}M")
            lines.append(msg)
            _notify_critical(msg)

            today = dt.date.today()
            cid = orders._make_cid("core", "vol-breakout", ticker, today)
            # Treat watchdog buy signals as core sleeve additions so they get
            # the same stop/trail policy as CANSLIM picks and stay visible to
            # SEPA / stop-loss checks.
            intent = orders.OrderIntent(
                symbol=ticker,
                notional=round(config.WATCHDOG_BUY_NOTIONAL, 2),
                side="buy",
                reason="volume-breakout-buy",
                tranche="core",
                client_order_id=cid,
                stop_pct=getattr(config, "STOP_LOSS_PCT", 0.08),
                trail_pct=getattr(config, "TRAILING_STOP_PCT", 0.12),
            )
            plan = orders.OrderPlan(buys=[intent], sells=[], holds=[])
            result = orders.execute_plan(plan, broker=broker, reason="watchdog-vol-breakout")
            if result.submitted:
                lines.append(f"  → submitted {ticker} ${config.WATCHDOG_BUY_NOTIONAL:,.0f}")
                _record_today_buy_signal(ticker)
            elif result.queued:
                # Queued for Telegram approval — counts as "fired", don't retry.
                lines.append(f"  → queued for TG approval")
                _record_today_buy_signal(ticker)
            elif result.skipped:
                reason_str = result.skipped[0][1] if result.skipped else "unknown"
                lines.append(f"  → skipped: {reason_str} (will retry next tick)")

        except Exception as exc:
            lines.append(f"  [buy-check error {ticker}]: {exc}")

    return lines


def _is_trading_hours() -> bool:
    """True if current wall-clock time falls within US market hours (9:30–16:00 ET, Mon–Fri).
    Thin wrapper for tests to monkeypatch; logic lives in timeutils."""
    from timeutils import is_rth_now
    return is_rth_now()


def _notify_critical(message: str) -> None:
    """Write a CRITICAL alert to the Telegram notification queue."""
    from notifications import append_notification
    append_notification({"source": "watchdog.intraday", "message": message})


def run_intraday() -> None:
    """Lightweight intraday check: SEPA exits + stop-loss monitoring.

    Designed to run every 5 min during market hours. Skips macro, news,
    volume, and rebalance checks — those run in the full daily pass.
    Exits immediately if outside trading hours so the cron can fire broadly.
    """
    if not _is_trading_hours():
        return

    now_str = dt.datetime.now().strftime("%H:%M")
    print(f"[{now_str}] Intraday watchdog check")

    broker = Broker(env=config.ALPACA_ENV)
    snap = snapshot(broker=broker)
    portfolio = {
        "positions": _as_legacy_positions(snap),
        "cash": snap.cash,
        "initial_capital": config.INITIAL_CAPITAL,
    }

    # Ensure trailing stops on any position missing one
    trail_result = orders.ensure_trailing_stops(broker)
    if trail_result.submitted:
        print(f"  Attached {len(trail_result.submitted)} trailing stop(s): "
              + ", ".join(o.symbol for o in trail_result.submitted))

    # SEPA exits — intraday wants real-time prices so R-tier / climax don't
    # fire ~5 min late on snap-stale state. One batched latest_quote loop
    # inside check_sepa_exits, not per-position.
    sepa_lines = check_sepa_exits(snap, broker, live_prices=True)
    for line in sepa_lines:
        print(f"  SEPA: {line}")

    # Price / stop-loss — use Alpaca latest_price for real-time intraday data
    price_alerts = check_price_moves(portfolio, broker=broker)
    critical = [a for a in price_alerts if "CRITICAL" in a[0]]
    warnings = [a for a in price_alerts if "WARNING" in a[0]]
    for a in critical + warnings:
        print(f"  {a[0]} [{a[1]}] {a[2]}")
    for a in critical:
        _notify_critical(f"[{a[1]}] {a[2]}")

    # Volume-breakout buy signals (screened stocks not yet in portfolio)
    buy_lines = check_buy_signals(snap, broker)
    for line in buy_lines:
        print(f"  BUY: {line}")

    if not sepa_lines and not price_alerts and not buy_lines:
        print("  OK")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--intraday" in args:
        run_intraday()
    elif "--quick" in args:
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
            print(f"  {pnl_icon} {r['ticker']:6s}  {r['shares']:>9.4f} × ${r['current']:>8.2f} = ${r['value']:>9.2f}  "
                  f"P&L: ${r['pnl']:>+8.2f} ({r['pnl_pct']:>+6.1f}%)")
        print(f"\n  Total: ${total_value:>10,.2f} | P&L: ${total_pnl:>+8.2f} ({total_pnl_pct:>+.1f}%)")
    elif "--history" in args:
        show_history()
    else:
        run_watchdog(quick=False)
