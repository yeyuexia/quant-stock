"""Planning-layer Protocols and shared I/O dataclasses.

These define the seams where deterministic rule-based logic lives today and
where LLM-backed implementations could drop in later. Concrete rule-based
implementations live in the same modules as the original functions they
wrap (rebalancer.py for target builders, planner.py for intent pricing),
not here. This module is pure types."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from orders import OrderIntent
from planner import PricingContext


@dataclass(frozen=True)
class TargetBuilderOutput:
    targets: dict                  # symbol -> weight (fraction of tranche_capital)
    capital: float                 # dollar capital for this tranche
    rationale: str                 # short human-readable explanation
    confidence: float              # 0..1; rule-based = 1.0; LLM populates meaningfully
    provider: str                  # "rule-based-core" | "rule-based-aggressive" | "llm-..." | ...


class TargetBuilder(Protocol):
    """Produces target weights for a tranche.

    Implementations may be pure rule-based (current behavior) or LLM-backed.
    `broker` is passed so implementations can read the live portfolio snapshot
    if they need to (e.g., to avoid proposing symbols already held).
    `tranche_capital` is the dollar budget the caller wants this tranche to
    target — derived from live Alpaca equity so the system compounds."""

    def build(self, *, tranche: str, broker, tranche_capital: float) -> TargetBuilderOutput: ...


@dataclass(frozen=True)
class IntentPricerOutput:
    priced: list                   # list[OrderIntent]
    provider: str                  # "rule-based" | "llm-..." | ...


class IntentPricer(Protocol):
    """Enriches raw OrderIntents with tier/decision_price/max_price/slice_count."""

    def price(self, intents, ctx: PricingContext) -> IntentPricerOutput: ...
