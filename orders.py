"""
Orders / policy layer. All Alpaca-touching decisions funnel through here:
  - state reconciliation (sync_state)
  - target-to-order diffing (reconcile_to_targets)
  - safety rails (HALT, daily caps, large-order gate)
  - pending-order queue (for Telegram approval)

Callers: rebalancer.py, watchdog.py, telegram bot.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from broker import (
    Broker, BrokerError, AccountSnapshot, Position, Order,
)
from fileio import atomic_write_json, atomic_append_text

# ── Types ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    notional: float
    side: str                       # "buy" | "sell"
    reason: str
    tranche: str
    client_order_id: str
    stop_pct: Optional[float] = None     # set on entries
    trail_pct: Optional[float] = None    # set on entries
    tier: Optional[str] = None
    decision_price: Optional[float] = None
    max_price: Optional[float] = None
    slice_count: Optional[int] = None


@dataclass(frozen=True)
class OrderPlan:
    buys: list[OrderIntent]
    sells: list[OrderIntent]
    holds: list[str]


@dataclass
class ExecutionResult:
    submitted: list[Order] = field(default_factory=list)
    queued: list[OrderIntent] = field(default_factory=list)
    skipped: list[tuple[OrderIntent, str]] = field(default_factory=list)
    deferred: list[OrderIntent] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioSnapshot:
    synced_at: str
    alpaca_env: str
    cash: float
    equity: float
    positions: list[dict]           # enriched: base position + tranche/entry_reason/stop_ids
    tranches: dict                  # {"core": {"last_rebalance": "YYYY-MM-DD"}, ...}

    def by_tranche(self, tranche: str) -> list[dict]:
        return [p for p in self.positions if p.get("tranche") == tranche]


# ── Client order ID ─────────────────────────────────────────────

def _make_cid(tranche: str, reason: str, symbol: str, today: dt.date) -> str:
    """Deterministic per (tranche, reason, symbol, day). Alpaca rejects duplicates,
    giving us free idempotency across cron re-runs within a day."""
    key = f"{tranche}|{reason}|{symbol}|{today.isoformat()}"
    h = hashlib.sha1(key.encode()).hexdigest()[:6]
    return f"{tranche}-{reason}-{symbol}-{today.strftime('%Y%m%d')}-{h}"


# ── Paths (overridable for tests) ───────────────────────────────

PORTFOLIO_PATH = os.path.join(os.path.dirname(__file__), "portfolio.json")
# orders-side event log: equity snapshots + closed-position events. Distinct
# from watchdog's daily_log.csv (which is a per-day portfolio summary with a
# completely different schema). Previously these shared `daily_log.csv` →
# silent schema collision broke show_history / pandas reads.
DAILY_LOG_PATH = os.path.join(os.path.dirname(__file__), ".cache", "orders_events.csv")

# ── Safety-rail paths (overridable for tests) ───────────────────

HALT_PATH = config.HALT_PATH
ENTRY_PIVOTS_PATH = config.ENTRY_PIVOTS_PATH
DAILY_TRADE_LOG = config.DAILY_TRADE_LOG
PENDING_ORDERS_PATH = config.PENDING_ORDERS_PATH


# ── sync_state ──────────────────────────────────────────────────

def _load_portfolio_cache() -> dict:
    if not os.path.exists(PORTFOLIO_PATH):
        return {"positions": [], "tranches": {
            "core": {"last_rebalance": None},
            "aggressive": {"last_rebalance": None},
        }}
    try:
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt cache (rare, e.g. crashed mid-write) — return empty rather
        # than crash the caller; sync_state will rebuild on next successful run.
        return {"positions": [], "tranches": {
            "core": {"last_rebalance": None},
            "aggressive": {"last_rebalance": None},
        }}


def _save_portfolio_cache(snap: PortfolioSnapshot):
    """Lock-protected atomic write so concurrent sync_state / cadence-bump /
    _set_climax_fired / tag_position can't lose each other's changes."""
    atomic_write_json(PORTFOLIO_PATH, asdict(snap))


def _load_entry_pivots() -> dict:
    """Load .cache/entry_pivots.json (the SEPA Phase 2 sidecar).

    Returns an empty dict when the file is missing or malformed — entry-pivot
    state is purely advisory; corruption should never block a watchdog run.
    """
    if not os.path.exists(ENTRY_PIVOTS_PATH):
        return {}
    try:
        with open(ENTRY_PIVOTS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_entry_pivots(pivots: dict) -> None:
    """Persist the entry-pivot sidecar with locking + atomic rename."""
    atomic_write_json(ENTRY_PIVOTS_PATH, pivots)


def _append_daily_log(line: str):
    """Append one CSV row to the orders events log (lock-protected)."""
    atomic_append_text(DAILY_LOG_PATH, line)


def sync_state(broker, *, alerts: Optional[list] = None) -> PortfolioSnapshot:
    """Fetch live positions from Alpaca, merge local metadata, write cache.

    Includes 1-retry inside on broker exceptions: a transient Alpaca blip
    would otherwise crash rebalancer.run() or watchdog.snapshot() with no
    auto-recovery (cron retries are 5min-24h away). Callers that want cache
    fallback on persistent failure should wrap this — see watchdog.snapshot.

    `alerts` (if provided) receives human-readable strings for anomalies:
      - positions on Alpaca we don't have metadata for → tranche 'unknown'
      - positions missing their bracket/trailing-stop orders
    """
    if alerts is None:
        alerts = []

    def _fetch_live():
        return (
            broker.get_account(),
            broker.get_positions(),
            broker.get_open_orders(),
        )

    last_exc = None
    for attempt in range(2):
        try:
            acc, live, open_orders = _fetch_live()
            break
        except BrokerError as e:
            last_exc = e
            if attempt == 0:
                import time as _time
                _time.sleep(0.3)
    else:
        # Both attempts failed
        raise last_exc  # type: ignore[misc]

    cache = _load_portfolio_cache()

    # Index local metadata by symbol
    old_meta = {p["symbol"]: p for p in cache.get("positions", [])}

    # Index open orders by (symbol, type) for bracket verification
    stops_by_symbol: dict[str, str] = {}
    trails_by_symbol: dict[str, str] = {}
    for o in open_orders:
        if o.type in ("stop", "stop_loss"):
            stops_by_symbol[o.symbol] = o.id
        elif o.type == "trailing_stop":
            trails_by_symbol[o.symbol] = o.id

    # Index stop orders for initial_stop_price lookup. The bracket's stop-loss
    # leg type is "stop"; standalone "stop_loss" type maps the same way.
    stop_orders_by_symbol: dict[str, Order] = {}
    for o in open_orders:
        if o.type in ("stop", "stop_loss"):
            stop_orders_by_symbol[o.symbol] = o

    positions: list[dict] = []
    live_symbols = {p.symbol for p in live}

    for p in live:
        meta = old_meta.get(p.symbol)
        cached_tranche = (meta or {}).get("tranche")
        # Adopt when the flag is on AND the position is either brand-new
        # (meta is None) OR already cached as 'unknown' (e.g. tagged before
        # adoption existed, or the flag was previously off). The latter makes
        # adoption self-healing: a stray 'unknown' is reclassified on the next
        # sync instead of staying frozen forever (rebalancer._system_equity
        # excludes unknown MV, so a stuck 'unknown' starves the rebalancer).
        if config.ADOPT_EXTERNAL_POSITIONS and (meta is None or cached_tranche == "unknown"):
            tranche = "aggressive" if p.symbol in config.ETF_LEVERAGED else "core"
            entry_reason = "adopted"
            origin = "external" if meta is None else "untagged"
            alerts.append(f"Adopted {origin} position {p.symbol} ({p.qty} sh) "
                          f"into {tranche} sleeve.")
        elif meta is None:
            alerts.append(f"Unknown position on Alpaca: {p.symbol} ({p.qty} sh). "
                          f"Tag with orders.tag_position('{p.symbol}', 'core'|'aggressive').")
            tranche = "unknown"
            entry_reason = "external"
        else:
            tranche = meta.get("tranche", "unknown")
            entry_reason = meta.get("entry_reason", "unknown")

        stop_id = stops_by_symbol.get(p.symbol)
        trail_id = trails_by_symbol.get(p.symbol)
        if stop_id is None and trail_id is None:
            alerts.append(f"No bracket/trailing stop attached to {p.symbol} — "
                          "stop protection inactive.")

        # ── SEPA initial-field snapshot ─────────────────────────
        existing_initial = (meta or {}).get("initial_entry_price")
        if existing_initial is None:
            # First sight (or pre-SEPA cache): snapshot now.
            initial_entry_price = float(p.avg_entry)
            initial_qty = float(p.qty)
            stop_ord = stop_orders_by_symbol.get(p.symbol)
            initial_stop_price = (
                float(stop_ord.stop_price)
                if stop_ord is not None and stop_ord.stop_price is not None
                else None
            )
            r_tier_filled: list[str] = []
            climax_fired = False
        else:
            # Immutable: preserve initial_*; check r_tier_filled appends below.
            initial_entry_price = (meta or {}).get("initial_entry_price")
            initial_qty = (meta or {}).get("initial_qty")
            initial_stop_price = (meta or {}).get("initial_stop_price")
            r_tier_filled = list((meta or {}).get("r_tier_filled", []))
            climax_fired = bool((meta or {}).get("climax_fired", False))

            # Narrow-immutability exception: backfill initial_stop_price when
            # it was None on first sight (no open stop attached yet) and a
            # stop order has since been placed. This makes SEPA pick up
            # protection for manually-stop-protected positions without
            # requiring an operator to clear initial_entry_price by hand.
            # Never overwrites an already-set initial_stop_price.
            if initial_stop_price is None:
                stop_ord = stop_orders_by_symbol.get(p.symbol)
                if stop_ord is not None and stop_ord.stop_price is not None:
                    initial_stop_price = float(stop_ord.stop_price)

            if (initial_qty and float(initial_qty) > 0
                    and initial_stop_price is not None
                    and tranche == "core"
                    and not climax_fired):
                # Proportional tolerance: at least 1 share, but at least 2%
                # of initial_qty so small fractional positions don't get all
                # tiers stamped at once (a 0.5-share initial with a flat 1.0
                # tolerance would credit every tier on first sight).
                EPS = max(1.0, float(initial_qty) * 0.02)
                cumulative_frac = 0.0
                for r, frac in config.SEPA_R_TIERS:
                    cumulative_frac += frac
                    label = f"{int(r)}R"
                    if label in r_tier_filled:
                        continue
                    threshold = float(initial_qty) * (1.0 - cumulative_frac) + EPS
                    if float(p.qty) <= threshold:
                        r_tier_filled.append(label)
                    else:
                        break

        # First-sight (or backfill) anchor date. The watchdog's trailing-stop
        # peak is measured from this date forward — without it, an adopted
        # legacy position would be measured against its full-history high and
        # could be liquidated on the first enforced tick despite being healthy
        # since we started managing it. Preserved across syncs once set.
        entry_date = (meta or {}).get("entry_date") or dt.date.today().isoformat()

        positions.append({
            "symbol": p.symbol,
            "shares": p.qty,
            "avg_entry": p.avg_entry,
            "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
            "tranche": tranche,
            "entry_reason": entry_reason,
            "entry_date": entry_date,
            "stop_order_id": stop_id,
            "trail_order_id": trail_id,
            "initial_entry_price": initial_entry_price,
            "initial_qty": initial_qty,
            "initial_stop_price": initial_stop_price,
            "r_tier_filled": r_tier_filled,
            "climax_fired": climax_fired,
        })

    # ── Starvation guardrail ────────────────────────────────────
    # Untagged ('unknown') positions are excluded from the rebalancer's
    # addressable capital (rebalancer._system_equity). If they dominate the
    # book, the rebalancer silently sizes itself to near-zero capital and stops
    # trading — exactly the failure this guards against.
    unknown_mv = sum(
        float(p.get("market_value", 0) or 0)
        for p in positions if p.get("tranche") == "unknown"
    )
    if acc.equity and unknown_mv / float(acc.equity) > config.UNKNOWN_MV_HALT_PCT:
        pct = 100.0 * unknown_mv / float(acc.equity)
        alerts.append(
            f"CRITICAL: untagged positions are {pct:.0f}% of equity — "
            f"rebalancer capital starved. Tag them or set "
            f"config.ADOPT_EXTERNAL_POSITIONS = True."
        )

    # Emit "closed" events for cached positions that vanished
    for sym, meta in old_meta.items():
        if sym not in live_symbols:
            _append_daily_log(f"{dt.datetime.now(dt.timezone.utc).isoformat()},CLOSED,{sym},"
                              f"{meta.get('tranche','unknown')},{meta.get('entry_reason','')}")

    tranches = cache.get("tranches", {
        "core": {"last_rebalance": None},
        "aggressive": {"last_rebalance": None},
    })

    snap = PortfolioSnapshot(
        synced_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        alpaca_env=getattr(broker, "env", "paper"),
        cash=acc.cash,
        equity=acc.equity,
        positions=positions,
        tranches=tranches,
    )
    _save_portfolio_cache(snap)

    # Append an equity snapshot
    _append_daily_log(f"{snap.synced_at},EQUITY,{snap.equity:.2f},{snap.cash:.2f}")
    return snap


# ── Stop / trailing-stop percentages per tranche ────────────────

def _tranche_stops(tranche: str) -> tuple[float, float]:
    if tranche == "aggressive":
        ap = config.AGGRESSIVE_PARAMS
        return ap["stop_loss_pct"], ap["trailing_stop_pct"]
    return config.STOP_LOSS_PCT, config.TRAILING_STOP_PCT


def _effective_stop_pct(symbol: str, tranche: str) -> float:
    """Per-symbol initial stop pct. ATR-scaled for core; fixed for aggressive.

    For core entries: clamp(ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close,
    ATR_STOP_FLOOR_PCT, base). Any data failure falls back to the tranche's
    base stop pct.

    Defensive / safe-haven symbols (BIL, SHY, IEF, TLT) skip ATR scaling and
    use the base stop: they're the cash-parking dumping ground, so a tight
    stop is conceptually wrong (you don't get stopped out of your hedge), and
    their near-zero volatility makes the ATR stop numerically absurd anyway
    (BIL's ATR/price ≈ 0.0005 → a 0.05% stop that fires on bid-ask noise).
    """
    base, _ = _tranche_stops(tranche)
    if tranche != "core":
        return base
    if symbol in getattr(config, "DEFENSIVE_SYMBOLS", ()):
        return base
    try:
        import data
        import indicators
        ohlcv = data.fetch_ohlcv([symbol], period="1y")
        high  = ohlcv["High"][symbol].dropna()
        low   = ohlcv["Low"][symbol].dropna()
        close = ohlcv["Close"][symbol].dropna()
        atr_val = indicators.atr(high, low, close, period=config.ATR_PERIOD)
        last = float(close.iloc[-1]) if not close.empty else 0.0
        if atr_val is None or atr_val <= 0 or last <= 0:
            return base
        atr_pct = config.ATR_STOP_MULTIPLIER * atr_val / last
        floor = getattr(config, "ATR_STOP_FLOOR_PCT", 0.0)
        # Clamp into [floor, base]: floor guards low-vol noise, base caps risk.
        return min(base, max(floor, atr_pct))
    except Exception:
        return base


# ── reconcile_to_targets ────────────────────────────────────────

def reconcile_to_targets(
    targets: dict[str, float],
    *,
    tranche: str,
    snapshot: PortfolioSnapshot,
    tranche_capital: float,
    today: dt.date,
) -> OrderPlan:
    """Diff target weights against current positions for the given tranche.

    targets: {symbol: fraction_of_tranche_capital}. Fractions summing to <1 leave the
    remainder in cash. Unknown-tranche positions are ignored (neither sold nor counted).
    Drifts smaller than `config.REBALANCE_BAND_PCT * tranche_capital` are treated as holds.
    """
    stop_pct, trail_pct = _tranche_stops(tranche)
    held = {p["symbol"]: p for p in snapshot.by_tranche(tranche)}

    # MAX_POSITION_PCT enforcement: defense in depth. Builders should respect
    # this themselves, but if one misbehaves we still cap the per-symbol
    # weight here rather than silently let it overweight. Defensive symbols
    # (BIL etc.) are exempt — they're the dumping ground for unallocated
    # capital so capping them defeats the macro hedge.
    max_pct = getattr(config, "MAX_POSITION_PCT", 1.0)
    defensive = set(getattr(config, "DEFENSIVE_SYMBOLS", ()))
    capped_targets: dict[str, float] = {}
    for sym, frac in targets.items():
        if sym not in defensive and frac > max_pct:
            print(f"[reconcile_to_targets] WARN: {sym} weight {frac:.2%} > "
                  f"MAX_POSITION_PCT {max_pct:.2%}, capping")
            capped_targets[sym] = max_pct
        else:
            capped_targets[sym] = frac

    target_dollars = {sym: frac * tranche_capital for sym, frac in capped_targets.items()}
    band = tranche_capital * config.REBALANCE_BAND_PCT

    buys: list[OrderIntent] = []
    sells: list[OrderIntent] = []
    holds: list[str] = []

    all_symbols = set(target_dollars) | set(held)
    for sym in sorted(all_symbols):
        current_mv = held.get(sym, {}).get("market_value", 0.0)
        target_mv = target_dollars.get(sym, 0.0)
        diff = target_mv - current_mv

        if abs(diff) < band and sym in held:
            holds.append(sym)
            continue

        reason = f"{tranche} rebalance"
        if diff > 0:
            cid = _make_cid(tranche, "rebalance", sym, today)
            buy_stop_pct = _effective_stop_pct(sym, tranche)
            buys.append(OrderIntent(
                symbol=sym, notional=round(diff, 2), side="buy",
                reason=reason, tranche=tranche, client_order_id=cid,
                stop_pct=buy_stop_pct, trail_pct=trail_pct,
            ))
        elif diff < 0:
            # Selling: notional is the amount to reduce by.
            cid = _make_cid(tranche, "rebalance-sell", sym, today)
            sells.append(OrderIntent(
                symbol=sym, notional=round(abs(diff), 2), side="sell",
                reason=reason, tranche=tranche, client_order_id=cid,
            ))

    return OrderPlan(buys=buys, sells=sells, holds=holds)


# ── Daily caps ──────────────────────────────────────────────────

DAILY_MAX_ORDERS = config.DAILY_MAX_ORDERS
DAILY_MAX_NOTIONAL = config.DAILY_MAX_NOTIONAL
LARGE_ORDER_THRESHOLD = config.LARGE_ORDER_THRESHOLD
PENDING_ORDER_TTL_HOURS = config.PENDING_ORDER_TTL_HOURS


def _today_key(now: Optional[dt.datetime] = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).date().isoformat()


def _load_daily_log() -> dict:
    if not os.path.exists(DAILY_TRADE_LOG):
        return {}
    try:
        with open(DAILY_TRADE_LOG) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt counter file (mid-write crash) — start fresh; safer to
        # under-count today than to crash all subsequent submits.
        return {}


def _save_daily_log(log: dict):
    """Lock-protected daily-cap counter persist."""
    atomic_write_json(DAILY_TRADE_LOG, log)


def _today_bucket(log: dict) -> dict:
    key = _today_key()
    if key not in log:
        log[key] = {"submitted_count": 0, "submitted_notional": 0.0, "deferred": []}
    return log[key]


def _try_consume_daily_cap(intent: OrderIntent, bucket: dict, notional: float
                           ) -> Optional[str]:
    """Check daily cap for `intent`. Returns None if accepted (caller should
    proceed), or a reason string if the intent must be deferred.

    Shared by execute_plan and submit_limit_slice so cap semantics stay
    consistent across both code paths.
    """
    if bucket["submitted_count"] >= DAILY_MAX_ORDERS:
        return f"daily order cap reached ({DAILY_MAX_ORDERS})"
    if bucket["submitted_notional"] + notional > DAILY_MAX_NOTIONAL:
        return (f"daily notional cap reached "
                f"(${bucket['submitted_notional']:,.0f} + ${notional:,.0f} > "
                f"${DAILY_MAX_NOTIONAL:,.0f})")
    return None


def _record_daily_cap(bucket: dict, notional: float) -> None:
    """Stamp a successful submission against the daily cap counter."""
    bucket["submitted_count"] += 1
    bucket["submitted_notional"] += notional


def _buy_priority(intent: OrderIntent) -> tuple:
    """Sort key for buy ordering: defensive symbols first, then smaller
    notional first (so a single large NVDA buy doesn't starve a small BIL
    hedge of cap / cash headroom)."""
    defensive = set(getattr(config, "DEFENSIVE_SYMBOLS", ()))
    is_defensive = intent.symbol in defensive
    # Lower tuple sorts first: (0, ...) before (1, ...)
    return (0 if is_defensive else 1, intent.notional, intent.symbol)


def execute_plan(plan: OrderPlan, *, broker, reason: str) -> ExecutionResult:
    """Run every intent through: HALT → market-open → cash-aware (greedy)
    → daily caps → large-order gate.

    Cash-aware is greedy not all-or-nothing: defensive buys (BIL/SHY/IEF/TLT
    by default) are sorted first, then smaller notional first; intents that
    fit within remaining cash get submitted, ones that don't get individually
    skipped. Prevents a single big buy from starving the macro hedge.

    Sells always run before buys (frees cash).
    """
    result = ExecutionResult()

    if os.path.exists(HALT_PATH):
        for i in list(plan.sells) + list(plan.buys):
            result.skipped.append((i, "HALT file present"))
        return result

    if not broker.is_market_open():
        for i in list(plan.sells) + list(plan.buys):
            result.skipped.append((i, "market closed — defer to next open"))
        return result

    # Snapshot cash up front; greedy fill below decrements it as buys consume.
    available_cash: Optional[float] = None
    if plan.buys and not getattr(config, "ALLOW_MARGIN", False):
        try:
            acc = broker.get_account()
            available_cash = float(acc.cash)
        except BrokerError:
            # Broker outage: fail-closed, reject all buys (sells still OK).
            for i in plan.buys:
                result.skipped.append((i, "cash-aware gate: broker account fetch failed"))
            plan = OrderPlan(buys=[], sells=list(plan.sells), holds=list(plan.holds))

    log = _load_daily_log()
    bucket = _today_bucket(log)
    pending = _load_pending()
    now = dt.datetime.now(dt.timezone.utc)

    # Sells first — they generate cash that buys can use.
    sells = list(plan.sells)
    sorted_buys = sorted(plan.buys, key=_buy_priority)
    intents = sells + sorted_buys

    for i in intents:
        # Cash-aware (greedy): for each buy, check if it fits in remaining
        # cash. If not, skip just this intent — don't poison sibling buys.
        if i.side == "buy" and available_cash is not None:
            if i.notional > available_cash:
                result.skipped.append((
                    i,
                    f"cash-aware gate: notional ${i.notional:,.2f} > "
                    f"remaining cash ${available_cash:,.2f}",
                ))
                continue

        # Daily cap first — deferred doesn't waste a pending slot
        cap_reason = _try_consume_daily_cap(i, bucket, i.notional)
        if cap_reason is not None:
            result.deferred.append(i)
            bucket["deferred"].append(asdict(i))
            continue

        # Large-order gate
        if i.notional >= LARGE_ORDER_THRESHOLD:
            pending.append(_intent_to_pending(i, now))
            result.queued.append(i)
            continue

        before = len(result.submitted)
        _submit_intent(broker, i, result)
        if len(result.submitted) > before:
            _record_daily_cap(bucket, i.notional)
            # Decrement cash budget for subsequent buys.
            if i.side == "buy" and available_cash is not None:
                available_cash -= i.notional

    _save_daily_log(log)
    _save_pending(pending)
    return result


def submit_limit_slice(
    intent: OrderIntent,
    *,
    limit_price: float,
    notional: float,
    broker,
) -> ExecutionResult:
    """Submit one slice of an intent as a marketable limit order.

    Enforces the same four safety rails as execute_plan: HALT, market-open,
    daily caps, large-order gate. Distinct `client_order_id` required per
    slice — callers suffix the parent cid with the slice index.
    """
    result = ExecutionResult()

    if os.path.exists(HALT_PATH):
        result.skipped.append((intent, "HALT file present"))
        return result

    try:
        if not broker.is_market_open():
            result.skipped.append((intent, "market closed — defer to next tick"))
            return result
    except BrokerError as e:
        result.skipped.append((intent, f"BrokerError: {e}"))
        return result

    log = _load_daily_log()
    bucket = _today_bucket(log)
    pending = _load_pending()

    cap_reason = _try_consume_daily_cap(intent, bucket, notional)
    if cap_reason is not None:
        result.deferred.append(intent)
        bucket["deferred"].append(asdict(intent))
        _save_daily_log(log)
        return result

    if notional >= LARGE_ORDER_THRESHOLD:
        sliced_intent = _intent_with_notional(intent, notional)
        pending.append(_intent_to_pending(sliced_intent, dt.datetime.now(dt.timezone.utc)))
        result.queued.append(sliced_intent)
        _save_pending(pending)
        return result

    try:
        o = broker.submit_limit(
            intent.symbol,
            notional=notional,
            side=intent.side,
            limit_price=limit_price,
            client_order_id=intent.client_order_id,
        )
        result.submitted.append(o)
        _record_daily_cap(bucket, notional)
    except BrokerError as e:
        result.skipped.append((intent, f"BrokerError: {e}"))

    _save_daily_log(log)
    return result


def _intent_with_notional(intent: OrderIntent, notional: float) -> OrderIntent:
    """Return a copy of intent with notional overridden (for per-slice tracking)."""
    from dataclasses import replace
    return replace(intent, notional=round(notional, 2))


def _submit_intent(broker, i: OrderIntent, result: ExecutionResult):
    """Submit a single intent via the appropriate broker method. Catches BrokerError.

    Bracket buys: percentage → absolute stop_price conversion happens here
    (policy layer), not in broker.py. We fetch the current price once and
    bind the stop in dollars before the order goes out — broker.submit_bracket
    is then pure I/O. The ±slippage between latest_price fetch and actual
    fill price still exists, but it's now in one place (here) and the docstring
    is honest about it.
    """
    try:
        if i.side == "buy":
            if i.stop_pct is not None and i.trail_pct is not None:
                # Compute absolute stop_price from policy's stop_pct.
                try:
                    last = float(broker.latest_price(i.symbol))
                except BrokerError as e:
                    result.skipped.append(
                        (i, f"latest_price for stop calc failed: {e}"))
                    return
                stop_price = round(last * (1 - float(i.stop_pct)), 2)
                o = broker.submit_bracket(
                    i.symbol, notional=i.notional,
                    stop_price=stop_price,
                    client_order_id=i.client_order_id,
                )
            else:
                o = broker.submit_market(
                    i.symbol, notional=i.notional, side="buy",
                    client_order_id=i.client_order_id,
                )
        else:
            o = broker.submit_market(
                i.symbol, notional=i.notional, side="sell",
                client_order_id=i.client_order_id,
            )
        result.submitted.append(o)
    except BrokerError as e:
        result.skipped.append((i, f"BrokerError: {e}"))


# ── Large-order pending queue ───────────────────────────────────

def _load_pending() -> list[dict]:
    if not os.path.exists(PENDING_ORDERS_PATH):
        return []
    try:
        with open(PENDING_ORDERS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt queue file (mid-write crash) — return empty list so callers
        # don't crash. New pending writes will rebuild it; Telegram bot will
        # see "no pending" for a moment until the next add.
        return []


def _save_pending(items: list[dict]):
    """Lock-protected pending-queue persist."""
    atomic_write_json(PENDING_ORDERS_PATH, items)


def _intent_to_pending(i: OrderIntent, now: dt.datetime) -> dict:
    return {
        "id": f"pend_{i.client_order_id}",
        "symbol": i.symbol,
        "notional": i.notional,
        "side": i.side,
        "stop_pct": i.stop_pct,
        "trail_pct": i.trail_pct,
        "reason": i.reason,
        "tranche": i.tranche,
        "client_order_id": i.client_order_id,
        "created": now.isoformat(),
        "expires": (now + dt.timedelta(hours=PENDING_ORDER_TTL_HOURS)).isoformat(),
    }


def _pending_to_intent(p: dict) -> OrderIntent:
    return OrderIntent(
        symbol=p["symbol"], notional=p["notional"], side=p["side"],
        reason=p["reason"], tranche=p["tranche"],
        client_order_id=p["client_order_id"],
        stop_pct=p.get("stop_pct"), trail_pct=p.get("trail_pct"),
    )


def list_pending() -> list[dict]:
    return _load_pending()


def reject_pending(pending_id: str) -> None:
    items = _load_pending()
    _save_pending([p for p in items if p["id"] != pending_id])


def approve_pending(pending_id: str, *, broker) -> ExecutionResult:
    """Re-runs HALT + market-open + daily cap checks before submitting.

    Non-destructive on transient failures: if HALT is active, market is closed,
    or daily caps are reached at approval time, the order remains in the queue
    so the user can re-approve later. Only expiry and successful submission
    remove the order from the queue.
    """
    result = ExecutionResult()
    items = _load_pending()
    target = next((p for p in items if p["id"] == pending_id), None)
    if target is None:
        result.skipped.append((None, f"pending id not found: {pending_id}"))  # type: ignore[arg-type]
        return result

    now = dt.datetime.now(dt.timezone.utc)
    expires = dt.datetime.fromisoformat(target["expires"])
    intent = _pending_to_intent(target)

    # Expiry: always remove from queue (order was going to disappear anyway).
    if now > expires:
        _save_pending([p for p in items if p["id"] != pending_id])
        result.skipped.append((intent, "pending order expired"))
        return result

    # Transient gates: leave the order in the queue so re-approval is possible.
    if os.path.exists(HALT_PATH):
        result.skipped.append((intent, "HALT file present — order remains pending"))
        return result

    if not broker.is_market_open():
        result.skipped.append((intent, "market closed — order remains pending"))
        return result

    log = _load_daily_log()
    bucket = _today_bucket(log)
    cap_reason = _try_consume_daily_cap(intent, bucket, intent.notional)
    if cap_reason is not None:
        result.skipped.append((intent, f"{cap_reason} — order remains pending"))
        return result

    # Cash-aware re-check: queued buys can sit for hours; cash might have
    # been spent since the queue. Only relevant for buys; sells are always
    # OK. Broker outage fails closed (we keep the order in the queue).
    if intent.side == "buy" and not getattr(config, "ALLOW_MARGIN", False):
        try:
            acc = broker.get_account()
            available_cash = float(acc.cash)
        except BrokerError as e:
            result.skipped.append(
                (intent, f"cash-aware re-check: broker fetch failed ({e}) — "
                          f"order remains pending"))
            return result
        if intent.notional > available_cash:
            result.skipped.append((
                intent,
                f"cash-aware re-check: notional ${intent.notional:,.2f} > "
                f"cash ${available_cash:,.2f} — order remains pending",
            ))
            return result

    # All checks passed; now remove from queue and submit.
    _save_pending([p for p in items if p["id"] != pending_id])
    before = len(result.submitted)
    _submit_intent(broker, intent, result)
    if len(result.submitted) > before:
        _record_daily_cap(bucket, intent.notional)
        _save_daily_log(log)
    return result


# ── ensure_trailing_stops ───────────────────────────────────────

def ensure_trailing_stops(broker) -> ExecutionResult:
    """Attach trailing-stop SELL orders to positions that lack them.

    `broker.submit_bracket` only attaches a fixed stop-loss leg — Alpaca does
    not allow combining trailing-stop + bracket natively. This helper runs
    after rebalance (and again from watchdog) to submit the trailing-stop leg
    for every known-tranche position that doesn't already have one attached.

    Respects HALT. Bypasses the daily-cap and large-order gates because
    trailing stops are protective orders: holding up protection on a cap is
    worse than exceeding the cap by one order.
    """
    result = ExecutionResult()
    if os.path.exists(HALT_PATH):
        return result

    try:
        positions = broker.get_positions()
        open_orders = broker.get_open_orders()
    except BrokerError as e:
        result.skipped.append((None, f"ensure_trailing_stops: {e}"))  # type: ignore[arg-type]
        return result

    trails_by_symbol = {o.symbol for o in open_orders if o.type == "trailing_stop"}

    cache = _load_portfolio_cache()
    meta_by_symbol = {p["symbol"]: p for p in cache.get("positions", [])}

    today = dt.date.today()
    for p in positions:
        if p.symbol in trails_by_symbol:
            continue
        meta = meta_by_symbol.get(p.symbol)
        if meta is None:
            continue
        tranche = meta.get("tranche", "unknown")
        if tranche == "unknown":
            tranche = "core"  # fallback: use core stop rules for untagged positions
        _, trail_pct = _tranche_stops(tranche)
        cid = _make_cid(tranche, "trail", p.symbol, today)
        try:
            o = broker.submit_trailing_stop(
                p.symbol, qty=p.qty,
                trail_percent=trail_pct,
                client_order_id=cid,
            )
            result.submitted.append(o)
        except BrokerError as e:
            # Duplicate client_order_id (already attached earlier today) is a no-op.
            msg = str(e)
            if "duplicate" in msg.lower():
                continue
            intent = OrderIntent(
                symbol=p.symbol, notional=0.0, side="sell",
                reason="trailing-stop attach", tranche=tranche,
                client_order_id=cid, stop_pct=None, trail_pct=trail_pct,
            )
            result.skipped.append((intent, f"BrokerError: {e}"))

    return result


# ── submit_exit ─────────────────────────────────────────────────

def submit_exit(symbol: str, *, reason: str, broker,
                current_price: Optional[float] = None) -> ExecutionResult:
    """Full-position exit. Writes a HIGH-tier intent with 150 bps tolerance to
    pending_plan.json — executor picks up on next tick and slices it out.

    Falls back to direct execute_plan if pending_plan already has a conflicting
    intent for this symbol (avoids double-selling).

    `current_price`: optional — caller (e.g., watchdog.check_sepa_exits with
    a batched live_price_by_symbol dict) can pre-fetch and pass it to save a
    redundant broker round-trip. None falls back to broker._latest_price.
    """
    from dataclasses import replace as _replace
    from pending_plan import load_plan, write_plan, IntentState, PendingPlan
    from baseline import capture_baseline

    cache = _load_portfolio_cache()
    meta = next((p for p in cache.get("positions", []) if p["symbol"] == symbol), None)
    if meta is None:
        result = ExecutionResult()
        result.skipped.append((None, f"no cached metadata for {symbol}"))  # type: ignore[arg-type]
        return result

    tranche = meta.get("tranche", "unknown")
    notional = float(meta["market_value"])
    cid = _make_cid(tranche, f"exit-{reason[:16]}", symbol, dt.date.today())
    raw = OrderIntent(
        symbol=symbol, notional=notional, side="sell",
        reason=reason, tranche=tranche, client_order_id=cid,
    )

    # Conflict: rebalance already targets this symbol → fall back to direct path.
    existing = load_plan()
    if existing is not None and any(s.intent.symbol == symbol for s in existing.intents):
        return execute_plan(
            OrderPlan(buys=[], sells=[raw], holds=[]),
            broker=broker, reason=reason,
        )

    # Price + tier + slice metadata for HIGH-tier macro exit
    if current_price is not None:
        last = float(current_price)
    else:
        try:
            last = broker._latest_price(symbol)
        except Exception:
            last = 0.0

    tolerance = config.MACRO_EXIT_TOLERANCE_BPS / 10_000.0
    floor = round(last * (1 - tolerance), 4) if last else 0.0
    priced = _replace(
        raw,
        tier="HIGH",
        decision_price=last,
        max_price=floor,
        slice_count=2,
    )

    # Write/append to pending plan
    if existing is None:
        baseline = capture_baseline()
        existing = PendingPlan(
            plan_id=f"exit-{dt.date.today().isoformat()}",
            tranche=tranche,
            created_at=dt.datetime.now(dt.timezone.utc),
            baseline=baseline,
            intents=[IntentState(intent=priced)],
        )
    else:
        existing.intents.append(IntentState(intent=priced))

    write_plan(existing)
    result = ExecutionResult()
    result.queued.append(priced)
    return result


def submit_partial_exit(symbol: str, *, fraction_of_initial: float,
                        reason: str, broker,
                        current_price: Optional[float] = None) -> ExecutionResult:
    """Partial-position exit. Notional = initial_qty × fraction × current_price.

    Writes a HIGH-tier intent (150 bps tolerance) to pending_plan.json so
    executor.py slices it through circuit breakers. Conflict with an
    existing pending_plan intent for the same symbol → falls back to
    direct execute_plan (mirrors submit_exit).

    `current_price`: optional — caller can pre-fetch (e.g., watchdog's
    batched live_price_by_symbol) to save a broker round-trip.

    No-ops (records skipped) when:
      - HALT file present
      - position metadata missing in cache
      - initial_qty missing (legacy / unsnapshotted)
    """
    from dataclasses import replace as _replace
    from pending_plan import load_plan, write_plan, IntentState, PendingPlan
    from baseline import capture_baseline

    result = ExecutionResult()
    if os.path.exists(HALT_PATH):
        intent = OrderIntent(
            symbol=symbol, notional=0.0, side="sell",
            reason=reason, tranche="core",
            client_order_id=_make_cid("core", f"partial-{reason[:12]}", symbol, dt.date.today()),
        )
        result.skipped.append((intent, "HALT file present"))
        return result

    cache = _load_portfolio_cache()
    meta = next((p for p in cache.get("positions", []) if p["symbol"] == symbol), None)
    if meta is None:
        result.skipped.append((None, f"no cached metadata for {symbol}"))  # type: ignore[arg-type]
        return result

    initial_qty = meta.get("initial_qty")
    if initial_qty is None or float(initial_qty) <= 0:
        result.skipped.append((None, f"{symbol}: initial_qty missing — cannot size partial"))  # type: ignore[arg-type]
        return result

    if current_price is None:
        try:
            current_price = float(broker._latest_price(symbol))
        except Exception as e:
            result.skipped.append((None, f"{symbol}: latest price unavailable: {e}"))  # type: ignore[arg-type]
            return result
    else:
        current_price = float(current_price)

    notional = round(float(initial_qty) * float(fraction_of_initial) * current_price, 2)
    tranche = meta.get("tranche", "core")
    cid = _make_cid(tranche, f"partial-{reason[:12]}", symbol, dt.date.today())
    raw = OrderIntent(
        symbol=symbol, notional=notional, side="sell",
        reason=reason, tranche=tranche, client_order_id=cid,
    )

    existing = load_plan()
    if existing is not None and any(s.intent.symbol == symbol for s in existing.intents):
        return execute_plan(
            OrderPlan(buys=[], sells=[raw], holds=[]),
            broker=broker, reason=reason,
        )

    tolerance = config.MACRO_EXIT_TOLERANCE_BPS / 10_000.0
    floor = round(current_price * (1 - tolerance), 4)
    priced = _replace(raw, tier="HIGH", decision_price=current_price,
                      max_price=floor, slice_count=2)

    if existing is None:
        baseline = capture_baseline()
        existing = PendingPlan(
            plan_id=f"sepa-{dt.date.today().isoformat()}",
            tranche=tranche,
            created_at=dt.datetime.now(dt.timezone.utc),
            baseline=baseline,
            intents=[IntentState(intent=priced)],
        )
    else:
        existing.intents.append(IntentState(intent=priced))

    write_plan(existing)
    result.queued.append(priced)
    return result


def cancel_position_trailing(symbol: str, *, broker) -> ExecutionResult:
    """Cancel any open trailing_stop orders on `symbol`. No-op when absent.

    Respects HALT. Bypasses daily-cap and large-order gates — cancellations
    carry no notional and should never queue for approval.
    """
    result = ExecutionResult()
    if os.path.exists(HALT_PATH):
        result.skipped.append((None, f"{symbol}: HALT file present, not cancelling trailing"))  # type: ignore[arg-type]
        return result

    try:
        open_orders = broker.get_open_orders()
    except BrokerError as e:
        result.skipped.append((None, f"cancel_position_trailing({symbol}): {e}"))  # type: ignore[arg-type]
        return result

    for o in open_orders:
        if o.symbol != symbol or o.type != "trailing_stop":
            continue
        try:
            broker.cancel_order(o.id)
        except BrokerError as e:
            result.skipped.append((None, f"cancel {o.id}: {e}"))  # type: ignore[arg-type]
    return result


# ── tag_position ────────────────────────────────────────────────

def tag_position(symbol: str, tranche: str, entry_reason: str = "manual") -> None:
    """Set tranche/entry_reason for a position in the cache.
    Use to label an 'unknown' position after a manual Alpaca trade.

    Lock-protected read-modify-write so a concurrent sync_state can't
    overwrite the tag.
    """
    if tranche not in ("core", "aggressive"):
        raise ValueError(f"tranche must be 'core' or 'aggressive', got {tranche!r}")

    from fileio import read_modify_write_json

    sentinel = {"_not_found": True}

    def mutate(cache):
        if "positions" not in cache:
            return sentinel
        for p in cache["positions"]:
            if p["symbol"] == symbol:
                p["tranche"] = tranche
                p["entry_reason"] = entry_reason
                return cache
        return sentinel

    result = read_modify_write_json(
        PORTFOLIO_PATH, mutate,
        default={"positions": [], "tranches": {}},
    )
    if result is sentinel:
        raise ValueError(f"{symbol} not in portfolio cache — run sync_state first")
