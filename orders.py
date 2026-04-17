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
DAILY_LOG_PATH = os.path.join(os.path.dirname(__file__), "daily_log.csv")

# ── Safety-rail paths (overridable for tests) ───────────────────

HALT_PATH = config.HALT_PATH
DAILY_TRADE_LOG = config.DAILY_TRADE_LOG
PENDING_ORDERS_PATH = config.PENDING_ORDERS_PATH


# ── sync_state ──────────────────────────────────────────────────

def _load_portfolio_cache() -> dict:
    if not os.path.exists(PORTFOLIO_PATH):
        return {"positions": [], "tranches": {
            "core": {"last_rebalance": None},
            "aggressive": {"last_rebalance": None},
        }}
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


def _save_portfolio_cache(snap: PortfolioSnapshot):
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(asdict(snap), f, indent=2, default=str)


def _append_daily_log(line: str):
    os.makedirs(os.path.dirname(DAILY_LOG_PATH), exist_ok=True) if os.path.dirname(DAILY_LOG_PATH) else None
    with open(DAILY_LOG_PATH, "a") as f:
        f.write(line + "\n")


def sync_state(broker, *, alerts: Optional[list] = None) -> PortfolioSnapshot:
    """Fetch live positions from Alpaca, merge local metadata, write cache.

    `alerts` (if provided) receives human-readable strings for anomalies:
      - positions on Alpaca we don't have metadata for → tranche 'unknown'
      - positions missing their bracket/trailing-stop orders
    """
    if alerts is None:
        alerts = []

    acc = broker.get_account()
    live = broker.get_positions()
    open_orders = broker.get_open_orders()
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

    positions: list[dict] = []
    live_symbols = {p.symbol for p in live}

    for p in live:
        meta = old_meta.get(p.symbol)
        if meta is None:
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

        positions.append({
            "symbol": p.symbol,
            "shares": p.qty,
            "avg_entry": p.avg_entry,
            "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
            "tranche": tranche,
            "entry_reason": entry_reason,
            "stop_order_id": stop_id,
            "trail_order_id": trail_id,
        })

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


# ── reconcile_to_targets ────────────────────────────────────────

_REBALANCE_BAND_PCT = 0.05   # ignore drifts smaller than 5% of tranche capital


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
    Drifts smaller than `_REBALANCE_BAND_PCT * tranche_capital` are treated as holds.
    """
    stop_pct, trail_pct = _tranche_stops(tranche)
    held = {p["symbol"]: p for p in snapshot.by_tranche(tranche)}

    target_dollars = {sym: frac * tranche_capital for sym, frac in targets.items()}
    band = tranche_capital * _REBALANCE_BAND_PCT

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
            buys.append(OrderIntent(
                symbol=sym, notional=round(diff, 2), side="buy",
                reason=reason, tranche=tranche, client_order_id=cid,
                stop_pct=stop_pct, trail_pct=trail_pct,
            ))
        elif diff < 0:
            # Selling: notional is the amount to reduce by.
            cid = _make_cid(tranche, "rebalance-sell", sym, today)
            sells.append(OrderIntent(
                symbol=sym, notional=round(abs(diff), 2), side="sell",
                reason=reason, tranche=tranche, client_order_id=cid,
            ))

    return OrderPlan(buys=buys, sells=sells, holds=holds)


# ── execute_plan (scaffolded with HALT only; caps/large-order added in later tasks)

def execute_plan(plan: OrderPlan, *, broker, reason: str) -> ExecutionResult:
    """Runs every intent through: HALT → market-open → daily caps → large-order gate."""
    result = ExecutionResult()
    intents = list(plan.sells) + list(plan.buys)   # sells first: free up buying power

    if os.path.exists(HALT_PATH):
        for i in intents:
            result.skipped.append((i, "HALT file present"))
        return result

    for i in intents:
        # Caps + large-order gate added in later tasks
        _submit_intent(broker, i, result)

    return result


def _submit_intent(broker, i: OrderIntent, result: ExecutionResult):
    """Submit a single intent via the appropriate broker method. Catches BrokerError."""
    try:
        if i.side == "buy":
            if i.stop_pct is not None and i.trail_pct is not None:
                o = broker.submit_bracket(
                    i.symbol, notional=i.notional,
                    stop_loss_pct=i.stop_pct, trailing_stop_pct=i.trail_pct,
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
