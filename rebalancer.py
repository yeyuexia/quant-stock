#!/usr/bin/env python3
"""
Scheduled rebalancer entry point.

Usage:
  python3 rebalancer.py --tranche core         # core tranche, mode-specific cadence
  python3 rebalancer.py --tranche aggressive   # aggressive tranche, 7-day cadence
  python3 rebalancer.py --dry-run              # print plan, submit nothing
  python3 rebalancer.py --force                # skip the "is it rebalance day?" gate
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys
from typing import Callable, Optional

import config
import orders
from broker import Broker


# ── Target builders ─────────────────────────────────────────────

def _build_core_targets() -> tuple[dict[str, float], float]:
    """Compose core-tranche targets from momentum + screener + macro.
    Returns (targets_dict, tranche_capital_dollars)."""
    from momentum import generate_signals
    from macro import macro_risk_adjustment

    capital = config.INITIAL_CAPITAL * (1 - config.AGGRESSIVE_TRANCHE_PCT)
    macro_adj = macro_risk_adjustment(1.0)
    etf_pct = config.ETF_ALLOCATION_PCT * macro_adj
    stock_pct = config.STOCK_ALLOCATION_PCT * macro_adj
    # Remainder goes to BIL as macro hedge (captured as a target).
    safe_pct = max(0.0, 1.0 - etf_pct - stock_pct - config.CASH_BUFFER_PCT)

    signals = generate_signals()
    targets: dict[str, float] = {}
    for sym, w in signals["holdings"]:
        targets[sym] = targets.get(sym, 0.0) + w * etf_pct

    # Stock sleeve: top-3 by composite score
    from screener import screen_stocks
    df = screen_stocks()
    if df is not None and not df.empty:
        top = df.head(3)
        per = stock_pct / max(1, len(top))
        for _, row in top.iterrows():
            targets[row["ticker"]] = targets.get(row["ticker"], 0.0) + per

    if safe_pct > 0.01:
        targets[config.SAFE_HAVEN] = targets.get(config.SAFE_HAVEN, 0.0) + safe_pct

    return targets, capital


def _build_aggressive_targets() -> tuple[dict[str, float], float]:
    """Top-N leveraged ETFs by momentum, equal-weighted. Uses ALL leveraged ETFs
    from config._ETF_LEVERAGED regardless of PORTFOLIO_MODE, because the
    aggressive tranche is always leveraged-ETF-only."""
    import pandas as pd
    from data import fetch_prices
    from momentum import _momentum_score

    capital = config.INITIAL_CAPITAL * config.AGGRESSIVE_TRANCHE_PCT
    top_n = config.AGGRESSIVE_PARAMS["momentum_top_n"]
    cash_buf = config.AGGRESSIVE_PARAMS["cash_buffer_pct"]

    leveraged = config._ETF_LEVERAGED
    prices = fetch_prices(leveraged + [config.SAFE_HAVEN], period="1y")
    rows = []
    for t in leveraged:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < config.SMA_FILTER_PERIOD:
            continue
        sma = s.rolling(config.SMA_FILTER_PERIOD).mean().iloc[-1]
        if s.iloc[-1] < sma:
            continue  # absolute momentum filter
        rows.append((t, _momentum_score(s, config.MOMENTUM_LOOKBACK_MONTHS)))

    rows.sort(key=lambda r: r[1], reverse=True)
    top = rows[:top_n]
    targets: dict[str, float] = {}
    if not top:
        targets[config.SAFE_HAVEN] = 1.0 - cash_buf
    else:
        w = (1.0 - cash_buf) / len(top)
        for sym, _ in top:
            targets[sym] = w
    return targets, capital


_TARGET_BUILDERS = {
    "core": _build_core_targets,
    "aggressive": _build_aggressive_targets,
}


# ── Entry point ─────────────────────────────────────────────────

def run(
    *,
    tranche: str,
    dry_run: bool,
    force: bool,
    broker,
    target_builder: Optional[Callable[[], tuple[dict[str, float], float]]] = None,
) -> Optional[orders.ExecutionResult]:
    """Execute (or dry-run) the rebalance for the given tranche.

    Returns:
      - None if skipped (not a rebalance day and not forced)
      - ExecutionResult on a real run
      - ExecutionResult with only `buys`/`sells` and no `submitted` on dry-run
    """
    if tranche not in ("core", "aggressive"):
        raise ValueError(f"tranche must be core|aggressive, got {tranche!r}")

    snap = orders.sync_state(broker, alerts=[])

    if not force:
        last = snap.tranches.get(tranche, {}).get("last_rebalance")
        if last:
            last_date = dt.date.fromisoformat(last)
            elapsed = (dt.date.today() - last_date).days
            if elapsed < config.REBALANCE_DAYS[tranche]:
                print(f"[{tranche}] not due: {elapsed}d since last rebalance "
                      f"(cadence {config.REBALANCE_DAYS[tranche]}d). Exiting.")
                return None

    builder = target_builder or _TARGET_BUILDERS[tranche]
    targets, tranche_capital = builder()

    plan = orders.reconcile_to_targets(
        targets, tranche=tranche, snapshot=snap,
        tranche_capital=tranche_capital, today=dt.date.today(),
    )

    _print_plan(tranche, targets, tranche_capital, plan)

    if dry_run:
        return orders.ExecutionResult()

    result = orders.execute_plan(plan, broker=broker, reason=f"{tranche} rebalance")

    # Attach trailing stops to freshly-opened positions. Small sleep lets
    # paper-account buys fill before we query positions.
    if result.submitted:
        import time
        time.sleep(2)
        trail_result = orders.ensure_trailing_stops(broker)
        for o in trail_result.submitted:
            result.submitted.append(o)
        for pair in trail_result.skipped:
            result.skipped.append(pair)

    # Only update last_rebalance if at least one order actually submitted.
    # A run fully blocked by HALT or caps should not satisfy the cadence gate.
    if result.submitted or result.queued:
        cache = orders._load_portfolio_cache()
        cache.setdefault("tranches", {}).setdefault(tranche, {})["last_rebalance"] = \
            dt.date.today().isoformat()
        import json
        with open(orders.PORTFOLIO_PATH, "w") as f:
            json.dump(cache, f, indent=2, default=str)

    _print_result(result)
    return result


def _print_plan(tranche: str, targets: dict, capital: float, plan: orders.OrderPlan):
    print(f"\n── {tranche.upper()} rebalance plan (capital ${capital:,.2f}) ──")
    print(f"Targets: {targets}")
    for i in plan.buys:
        print(f"  BUY   {i.symbol:6s} ${i.notional:>10,.2f}   (stop={i.stop_pct} trail={i.trail_pct})")
    for i in plan.sells:
        print(f"  SELL  {i.symbol:6s} ${i.notional:>10,.2f}")
    if plan.holds:
        print(f"  HOLD  {', '.join(plan.holds)}")


def _print_result(result: orders.ExecutionResult):
    print(f"\nSubmitted: {len(result.submitted)}  "
          f"Queued (Telegram): {len(result.queued)}  "
          f"Deferred: {len(result.deferred)}  "
          f"Skipped: {len(result.skipped)}")
    for o in result.submitted:
        print(f"  ✓ {o.symbol} {o.side} ${o.notional} ({o.id})")
    for i in result.queued:
        print(f"  ⏳ {i.symbol} ${i.notional} queued for Telegram approval")
    for i, msg in result.skipped:
        sym = i.symbol if i is not None else "?"
        print(f"  ✗ {sym}: {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tranche", required=True, choices=["core", "aggressive", "both"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    broker = Broker(env=config.ALPACA_ENV)
    tranches = ["core", "aggressive"] if args.tranche == "both" else [args.tranche]
    for t in tranches:
        run(tranche=t, dry_run=args.dry_run, force=args.force, broker=broker)


if __name__ == "__main__":
    main()
