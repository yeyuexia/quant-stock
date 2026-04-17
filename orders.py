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
