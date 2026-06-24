"""Pending-plan persistence: read/write/clear .cache/pending_plan.json.

A PendingPlan represents one tranche's rebalance for one day. The executor
reads it on each tick, submits slices, updates per-intent state, and writes
it back. Plans are discarded at end-of-day or on next rebalancer run.

All disk writes go through fileio.atomic_write_json (fcntl lock + tmp+rename)
so the executor's read-modify-write cycle never collides with a concurrent
rebalancer.run() that's appending more intents.
"""
from __future__ import annotations
import dataclasses as _dc
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from fileio import atomic_write_json
from orders import OrderIntent

_log = logging.getLogger(__name__)

PENDING_PLAN_PATH = config.PENDING_PLAN_PATH

# OrderIntent fields that the JSON might persist — used to filter unknown
# fields if a plan was written by an older schema version.
_ORDER_INTENT_FIELDS = {f.name for f in _dc.fields(OrderIntent)}


@dataclass
class Baseline:
    spy: float
    vix: float
    macro_score: float
    news_cursor_at: dt.datetime


@dataclass
class IntentState:
    intent: OrderIntent
    status: str = "active"
    notional_filled: float = 0.0
    slices_submitted: int = 0
    last_client_order_id: Optional[str] = None
    last_limit_price: Optional[float] = None
    abort_reason: Optional[str] = None


@dataclass
class PendingPlan:
    plan_id: str
    tranche: str
    created_at: dt.datetime
    baseline: Baseline
    intents: list[IntentState]
    breakers_tripped: list[str] = field(default_factory=list)
    # Cross-tick news-hit dedupe state: {title_hash -> ISO timestamp of last
    # observation}. Pruned each tick to the news dedupe window so it doesn't
    # grow unbounded over the trading day.
    news_hits_seen: dict = field(default_factory=dict)


def write_plan(plan: PendingPlan) -> None:
    """Atomic, lock-protected write of the pending plan."""
    atomic_write_json(PENDING_PLAN_PATH, _plan_to_dict(plan))


def load_plan() -> Optional[PendingPlan]:
    """Read pending plan; returns None if missing or corrupt.

    Corrupt files used to crash the executor — now we log a warning and
    return None, letting the executor treat it like "no plan today" and
    the next rebalancer run will rebuild a clean plan.
    """
    if not os.path.exists(PENDING_PLAN_PATH):
        return None
    try:
        with open(PENDING_PLAN_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        _log.warning(
            "load_plan: %s unreadable (%s); treating as no plan",
            PENDING_PLAN_PATH, e,
        )
        return None
    try:
        return _dict_to_plan(data)
    except (KeyError, TypeError, ValueError) as e:
        _log.warning(
            "load_plan: %s schema mismatch (%s); treating as no plan",
            PENDING_PLAN_PATH, e,
        )
        return None


def clear_plan() -> None:
    if os.path.exists(PENDING_PLAN_PATH):
        try:
            os.remove(PENDING_PLAN_PATH)
        except OSError as e:
            _log.warning("clear_plan: remove failed: %s", e)


def _plan_to_dict(plan: PendingPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "tranche": plan.tranche,
        "created_at": plan.created_at.isoformat(),
        "baseline": {
            "spy": plan.baseline.spy,
            "vix": plan.baseline.vix,
            "macro_score": plan.baseline.macro_score,
            "news_cursor_at": plan.baseline.news_cursor_at.isoformat(),
        },
        "intents": [
            {
                "intent": asdict(s.intent),
                "status": s.status,
                "notional_filled": s.notional_filled,
                "slices_submitted": s.slices_submitted,
                "last_client_order_id": s.last_client_order_id,
                "last_limit_price": s.last_limit_price,
                "abort_reason": s.abort_reason,
            }
            for s in plan.intents
        ],
        "breakers_tripped": list(plan.breakers_tripped),
        "news_hits_seen": dict(plan.news_hits_seen),
    }


def _dict_to_plan(d: dict) -> PendingPlan:
    bl = d["baseline"]
    baseline = Baseline(
        spy=bl["spy"], vix=bl["vix"], macro_score=bl["macro_score"],
        news_cursor_at=dt.datetime.fromisoformat(bl["news_cursor_at"]),
    )
    intents = []
    for s in d["intents"]:
        raw_intent = s["intent"]
        # Filter unknown fields so a plan written by an older OrderIntent
        # schema (or a future one with extra fields) still loads. Unknown
        # fields used to raise TypeError from OrderIntent(**...).
        intent_kwargs = {k: v for k, v in raw_intent.items()
                         if k in _ORDER_INTENT_FIELDS}
        i = OrderIntent(**intent_kwargs)
        intents.append(IntentState(
            intent=i,
            status=s.get("status", "active"),
            notional_filled=s.get("notional_filled", 0.0),
            slices_submitted=s.get("slices_submitted", 0),
            last_client_order_id=s.get("last_client_order_id"),
            last_limit_price=s.get("last_limit_price"),
            abort_reason=s.get("abort_reason"),
        ))
    return PendingPlan(
        plan_id=d["plan_id"],
        tranche=d["tranche"],
        created_at=dt.datetime.fromisoformat(d["created_at"]),
        baseline=baseline,
        intents=intents,
        breakers_tripped=d.get("breakers_tripped", []),
        news_hits_seen=d.get("news_hits_seen", {}),
    )
