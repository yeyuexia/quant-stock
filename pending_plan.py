"""Pending-plan persistence: read/write/clear .cache/pending_plan.json.

A PendingPlan represents one tranche's rebalance for one day. The executor
reads it on each tick, submits slices, updates per-intent state, and writes
it back. Plans are discarded at end-of-day or on next rebalancer run.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from orders import OrderIntent

PENDING_PLAN_PATH = config.PENDING_PLAN_PATH


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


def write_plan(plan: PendingPlan) -> None:
    os.makedirs(os.path.dirname(PENDING_PLAN_PATH), exist_ok=True)
    with open(PENDING_PLAN_PATH, "w") as f:
        json.dump(_plan_to_dict(plan), f, indent=2, default=str)


def load_plan() -> Optional[PendingPlan]:
    if not os.path.exists(PENDING_PLAN_PATH):
        return None
    with open(PENDING_PLAN_PATH) as f:
        data = json.load(f)
    return _dict_to_plan(data)


def clear_plan() -> None:
    if os.path.exists(PENDING_PLAN_PATH):
        os.remove(PENDING_PLAN_PATH)


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
    }


def _dict_to_plan(d: dict) -> PendingPlan:
    bl = d["baseline"]
    baseline = Baseline(
        spy=bl["spy"], vix=bl["vix"], macro_score=bl["macro_score"],
        news_cursor_at=dt.datetime.fromisoformat(bl["news_cursor_at"]),
    )
    intents = []
    for s in d["intents"]:
        i = OrderIntent(**s["intent"])
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
    )
