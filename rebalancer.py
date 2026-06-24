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
import os
import sys
from typing import Callable, Optional

import config
import orders
from broker import Broker


# ── Target builders ─────────────────────────────────────────────

def _system_equity(snap: "orders.PortfolioSnapshot") -> float:
    """Total account equity minus the market value of unknown-tranche positions.

    Unknown positions are ones the system didn't open (manual trades, legacy
    holdings) — they shouldn't be counted toward the budget that core/aggressive
    sleeves try to allocate against.
    """
    unknown_mv = sum(
        float(p.get("market_value", 0) or 0)
        for p in snap.by_tranche("unknown")
    )
    return max(0.0, float(snap.equity) - unknown_mv)


def _build_core_targets(tranche_capital: float) -> tuple[dict[str, float], float]:
    """Compose core-tranche targets from momentum + screener + macro.

    Returns (targets_dict, tranche_capital). targets are fractions of
    tranche_capital that should sum to ≤ 1.0; any unallocated remainder sits
    in cash. The macro-shrunk equity portion has a hard BIL floor so we never
    silently drop the stock sleeve into cash when the screener returns nothing.
    """
    from momentum import generate_signals
    from macro import macro_risk_adjustment

    macro_adj = macro_risk_adjustment(1.0)
    etf_pct = config.ETF_ALLOCATION_PCT * macro_adj
    stock_pct = config.STOCK_ALLOCATION_PCT * macro_adj

    # Load current core holdings once so generate_signals can apply hysteresis
    # and the stock sleeve can skip already-held pivot refreshes.
    cache = orders._load_portfolio_cache()
    held_core = {
        p["symbol"] for p in cache.get("positions", [])
        if p.get("tranche") == "core"
    }

    targets: dict[str, float] = {}

    # ── ETF sleeve (with hysteresis) ───────────────────────────
    signals = generate_signals(held_etfs=held_core)
    for sym, w in signals["holdings"]:
        targets[sym] = targets.get(sym, 0.0) + w * etf_pct

    # ── Stock sleeve (with empty/sparse → BIL fallback) ────────
    from screener import screen_stocks
    df = screen_stocks()
    stock_allocated = 0.0
    if df is not None and not df.empty:
        top = df.head(config.STOCK_SLEEVE_TOP_N)
        n = len(top)
        # Even-weight, capped at MAX_POSITION_PCT to avoid single-stock concentration
        # when the screener returns fewer than STOCK_SLEEVE_TOP_N picks.
        per = min(stock_pct / n, config.MAX_POSITION_PCT)

        pivots = orders._load_entry_pivots()
        today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        pivots_dirty = False
        import pandas as _pd
        for _, row in top.iterrows():
            sym = row["ticker"]
            targets[sym] = targets.get(sym, 0.0) + per
            stock_allocated += per
            if sym in held_core:
                continue  # keep existing pivot
            base_hi = row.get("base_hi")
            price = row.get("price")
            if base_hi is not None and not _pd.isna(base_hi):
                pivot = float(base_hi)
            elif price is not None and not _pd.isna(price):
                # Fallback: stock passed RS/ADR/EMA but didn't form a clean VCP base.
                # Use the screening close so SEPA failed-breakout still has a reference.
                pivot = float(price)
            else:
                continue  # no usable reference at all
            pivots[sym] = {"pivot": pivot, "entry_date": today_str}
            pivots_dirty = True
        if pivots_dirty:
            orders._save_entry_pivots(pivots)

    # Anything in stock_pct not actually allocated (empty screener, capped picks,
    # short top list) gets rolled into the defensive BIL bucket — never silently
    # left in cash.
    stock_unallocated = max(0.0, stock_pct - stock_allocated)

    # Cash buffer only materializes when macro_adj is low enough that
    # etf_pct + stock_pct + buffer < 1.0. In healthy regimes (macro_adj ≈ 1.0)
    # the configured ETF+stock allocations are typically ≥ 1.0 so the buffer
    # is absorbed — by design.
    safe_pct = max(0.0, 1.0 - etf_pct - stock_pct - config.CASH_BUFFER_PCT)
    bil_total = safe_pct + stock_unallocated
    if bil_total > 0.005:
        targets[config.SAFE_HAVEN] = targets.get(config.SAFE_HAVEN, 0.0) + bil_total

    return targets, tranche_capital


def _build_aggressive_targets(tranche_capital: float) -> tuple[dict[str, float], float]:
    """Top-N leveraged ETFs by momentum, equal-weighted.

    Uses ALL leveraged ETFs from config.ETF_LEVERAGED regardless of
    PORTFOLIO_MODE — the aggressive tranche is always leveraged-ETF-only.
    Applies hysteresis: a held leveraged ETF that slips out of the top-N is
    kept as long as it stays within top-(N + hysteresis_depth) AND still
    above its 200-day SMA. Prevents whipsaw when rank flickers.
    """
    from data import fetch_prices
    from momentum import _momentum_score

    top_n = config.AGGRESSIVE_PARAMS["momentum_top_n"]
    cash_buf = config.AGGRESSIVE_PARAMS["cash_buffer_pct"]
    hyst_depth = config.AGGRESSIVE_PARAMS.get("hysteresis_depth", 0)

    cache = orders._load_portfolio_cache()
    held_agg = {
        p["symbol"] for p in cache.get("positions", [])
        if p.get("tranche") == "aggressive"
    }

    leveraged = config.ETF_LEVERAGED
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
    # Hysteresis: rows ranked top_n+1..top_n+hyst_depth that are currently held
    # also keep their slot (SMA filter was already applied above).
    sticky = [r for r in rows[top_n : top_n + hyst_depth] if r[0] in held_agg]
    selected = top + sticky

    targets: dict[str, float] = {}
    if not selected:
        targets[config.SAFE_HAVEN] = 1.0 - cash_buf
    else:
        # Same weight cap as core: divide by max(len, top_n) so sticky picks
        # don't push total above 1 - cash_buffer.
        deployable = 1.0 - cash_buf
        w = deployable / max(len(selected), top_n)
        for sym, _ in selected:
            targets[sym] = w
        remainder = deployable - w * len(selected)
        if remainder > 0.005:
            targets[config.SAFE_HAVEN] = targets.get(config.SAFE_HAVEN, 0.0) + remainder
    return targets, tranche_capital


class RuleBasedCoreTargetBuilder:
    """Rule-based target builder for the core tranche.

    Implements planning.TargetBuilder. Caller supplies tranche_capital so
    the system can compound — capital is no longer pinned to INITIAL_CAPITAL."""

    def build(self, *, tranche, broker, tranche_capital):
        from planning import TargetBuilderOutput
        targets, capital = _build_core_targets(tranche_capital)
        return TargetBuilderOutput(
            targets=targets,
            capital=capital,
            rationale="core: dual-momentum ETF rotation + CANSLIM screen + macro overlay + BIL safe-haven",
            confidence=1.0,
            provider="rule-based-core",
        )


class RuleBasedAggressiveTargetBuilder:
    """Rule-based target builder for the aggressive tranche.

    Implements planning.TargetBuilder. Caller supplies tranche_capital so
    the system can compound — capital is no longer pinned to INITIAL_CAPITAL."""

    def build(self, *, tranche, broker, tranche_capital):
        from planning import TargetBuilderOutput
        targets, capital = _build_aggressive_targets(tranche_capital)
        return TargetBuilderOutput(
            targets=targets,
            capital=capital,
            rationale="aggressive: top-N leveraged ETFs by own momentum scoring, cash buffer",
            confidence=1.0,
            provider="rule-based-aggressive",
        )


_TARGET_BUILDERS = {
    "core": RuleBasedCoreTargetBuilder(),
    "aggressive": RuleBasedAggressiveTargetBuilder(),
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

    # Dynamic tranche capital: tracks Alpaca account equity (minus unknown-tranche
    # positions) so the system compounds. Falls back to INITIAL_CAPITAL × split
    # only if the system equity calc produces nothing (defensive — shouldn't fire
    # in normal operation).
    system_equity = _system_equity(snap)
    if system_equity <= 0:
        system_equity = config.INITIAL_CAPITAL
    if tranche == "aggressive":
        tranche_capital = system_equity * config.AGGRESSIVE_TRANCHE_PCT
    else:
        tranche_capital = system_equity * (1 - config.AGGRESSIVE_TRANCHE_PCT)

    builder = target_builder or _TARGET_BUILDERS[tranche]
    # Accept both Protocol (.build(...)) and callable (legacy tests) forms.
    if hasattr(builder, "build"):
        result = builder.build(tranche=tranche, broker=broker, tranche_capital=tranche_capital)
        targets, tranche_capital = result.targets, result.capital
    else:
        targets, tranche_capital = builder()

    plan = orders.reconcile_to_targets(
        targets, tranche=tranche, snapshot=snap,
        tranche_capital=tranche_capital, today=dt.date.today(),
    )

    _print_plan(tranche, targets, tranche_capital, plan)

    if dry_run:
        return orders.ExecutionResult()

    # Split plan into (direct-submit tiny orders) and (intraday-executor orders)
    tiny_intents = []
    intraday_intents = []
    for i in (list(plan.buys) + list(plan.sells)):
        if i.notional < config.PLANNER_DIRECT_SUBMIT_THRESHOLD:
            tiny_intents.append(i)
        else:
            intraday_intents.append(i)

    tiny_plan = orders.OrderPlan(
        buys=[i for i in tiny_intents if i.side == "buy"],
        sells=[i for i in tiny_intents if i.side == "sell"],
        holds=[],
    )
    result = orders.execute_plan(tiny_plan, broker=broker, reason=f"{tranche} rebalance (tiny)")

    if intraday_intents:
        _write_pending_plan(tranche, intraday_intents, broker=broker)

    if result.submitted:
        import time
        time.sleep(2)
        trail = orders.ensure_trailing_stops(broker)
        result.submitted.extend(trail.submitted)
        result.skipped.extend(trail.skipped)

    # Cadence bump: commit today if either tiny submissions happened OR intraday plan written.
    if result.submitted or result.queued or intraday_intents:
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


def _write_pending_plan(tranche, intents, *, broker):
    """Enrich intents with tier/max_price/slice_count; capture baseline; persist.

    Merges with any existing plan on disk so that running --tranche both
    (core then aggressive) preserves both tranches' intents.  A same-tranche
    re-run is idempotent: the current tranche's intents are replaced, not
    duplicated.  The baseline and created_at are kept from the first writer.
    """
    from baseline import capture_baseline
    from planner import build_priced_intents, PricingContext
    from pending_plan import PendingPlan, IntentState, load_plan, write_plan

    # Only capture baseline when we're creating a brand-new plan; reuse the
    # existing baseline on merge (first-writer wins — that's the day's reference).
    # Discard stale plans from prior trading days — prices are too outdated to merge.
    existing = load_plan()
    if existing is not None:
        # created_at is stored as UTC; compare in UTC so the discard threshold
        # doesn't flip across UTC midnight when local-time != UTC-date (e.g.
        # HKT 00:00–08:00 local is still the previous UTC date).
        plan_date = existing.created_at.date() if hasattr(existing.created_at, "date") \
            else dt.date.fromisoformat(str(existing.created_at)[:10])
        if plan_date < dt.datetime.now(dt.timezone.utc).date():
            existing = None
    if existing is None:
        baseline = capture_baseline()
    else:
        baseline = existing.baseline

    ranks = {}
    asset_class = {}
    decision_prices = {}
    symbols = [i.symbol for i in intents]

    try:
        from momentum import generate_signals
        cache = orders._load_portfolio_cache()
        held_core = {
            p["symbol"] for p in cache.get("positions", [])
            if p.get("tranche") == "core"
        }
        sig = generate_signals(held_etfs=held_core)
        for ticker, _w, rank in sig.get("holdings_ranked", []):
            if ticker in symbols:
                ranks[ticker] = rank
                asset_class[ticker] = "etf"
    except Exception:
        pass

    try:
        from screener import screen_stocks
        df = screen_stocks()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                t = row["ticker"]
                if t in symbols:
                    ranks[t] = int(row["rank"])
                    asset_class[t] = "stock"
    except Exception:
        pass

    for s in symbols:
        asset_class.setdefault(s, "etf")
        # Aggressive-tranche picks are always top-N by their own leveraged-ETF
        # momentum scoring (_build_aggressive_targets); they don't appear in
        # momentum.generate_signals' holdings_ranked. Treat every aggressive
        # pick as rank 1 so it gets HIGH tier with the wider tolerance that
        # leveraged-ETF volatility warrants.
        if tranche == "aggressive":
            ranks[s] = 1
        else:
            ranks.setdefault(s, 99)

    missing_prices = []
    for s in symbols:
        try:
            decision_prices[s] = broker._latest_price(s)
        except Exception:
            missing_prices.append(s)

    if missing_prices:
        print(f"\n── WARN: no decision price for {missing_prices}; "
              f"dropping from pending plan. Tomorrow's rebalance will re-evaluate.")
        intents = [i for i in intents if i.symbol not in missing_prices]
        if not intents:
            print("── No intents left to plan; skipping pending_plan write.")
            return

    ctx = PricingContext(
        ranks=ranks, asset_class=asset_class,
        decision_prices=decision_prices, tranche=tranche,
    )
    priced = build_priced_intents(intents, ctx)

    # Merge with any existing plan on disk.
    if existing is not None:
        # Drop intents from this tranche (same-day re-run is idempotent);
        # keep intents from other tranches.
        kept_states = [s for s in existing.intents if s.intent.tranche != tranche]
        new_states = [IntentState(intent=i) for i in priced]
        all_states = kept_states + new_states
        # Plan-level tranche becomes "mixed" if more than one tranche is present.
        tranches_present = {s.intent.tranche for s in all_states}
        plan_tranche = "mixed" if len(tranches_present) > 1 else tranche
        plan_id_prefix = "multi" if plan_tranche == "mixed" else tranche
        plan = PendingPlan(
            plan_id=f"{plan_id_prefix}-{dt.date.today().isoformat()}",
            tranche=plan_tranche,
            created_at=existing.created_at,          # preserve first-writer creation time
            baseline=existing.baseline,              # preserve first-writer baseline
            intents=all_states,
            breakers_tripped=existing.breakers_tripped,
        )
    else:
        plan = PendingPlan(
            plan_id=f"{tranche}-{dt.date.today().isoformat()}",
            tranche=tranche,
            created_at=dt.datetime.now(dt.timezone.utc),
            baseline=baseline,
            intents=[IntentState(intent=i) for i in priced],
        )
    write_plan(plan)
    print(f"\n── Pending plan written: {len(plan.intents)} intents "
          f"(tranche={plan.tranche}), baseline SPY={baseline.spy:.2f} "
          f"VIX={baseline.vix:.2f} macro={baseline.macro_score:+.3f}")
    _notify_plan_to_telegram(tranche, priced, baseline)


def _notify_plan_to_telegram(tranche: str, intents: list, baseline) -> None:
    """Append a new-plan summary to TELEGRAM_NOTIFY_PATH. No-op if unset."""
    if not intents:
        return
    from notifications import append_notification

    lines = [f"📋 New Plan — {tranche} tranche"]
    lines.append(
        f"Baseline: SPY={baseline.spy:.2f}  VIX={baseline.vix:.2f}  "
        f"macro={baseline.macro_score:+.2f}"
    )
    lines.append("")
    total = 0.0
    for i in intents:
        lines.append(
            f"  {i.side.upper():4s} {i.symbol:<6s} ${i.notional:>9,.0f}   "
            f"(max {i.max_price:.2f}, slices {i.slice_count}, {i.tier})"
        )
        total += i.notional
    lines.append("")
    lines.append(f"Total: ${total:,.0f} across {len(intents)} intents")

    append_notification({
        "source": "rebalancer",
        "tranche": tranche,
        "message": "\n".join(lines),
    })


def main():
    # Load .env so ALPACA_API_KEY / ALPACA_API_SECRET are available when
    # launched by launchd (which doesn't inherit the user's shell environment).
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    except ImportError:
        pass  # python-dotenv not installed; rely on env vars already being set

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
