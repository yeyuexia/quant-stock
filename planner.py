"""Enriches raw OrderIntent lists with tier, decision_price, max_price, slice_count.

Called by rebalancer.py after reconcile_to_targets produces the bare plan.
Pure function — no I/O, no broker calls. All the I/O (price lookup, rank
computation) happens in the caller and is passed via PricingContext.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Iterable

import config
from orders import OrderIntent


@dataclass(frozen=True)
class PricingContext:
    ranks: dict[str, int]
    asset_class: dict[str, str]
    decision_prices: dict[str, float]
    tranche: str


def build_priced_intents(
    intents: Iterable[OrderIntent],
    ctx: PricingContext,
) -> list[OrderIntent]:
    out = []
    for raw in intents:
        tier = _tier_for(raw.symbol, ctx)
        asset = ctx.asset_class.get(raw.symbol, "stock")
        bps = config.EXECUTION_TIERS[tier][f"{asset}_bps"]
        tolerance = bps / 10_000.0
        if ctx.tranche == "aggressive":
            tolerance *= config.AGGRESSIVE_TIER_MULTIPLIER

        price = ctx.decision_prices.get(raw.symbol)
        if price is None or price <= 0:
            out.append(raw)
            continue

        if raw.side == "buy":
            max_price = round(price * (1 + tolerance), 4)
        else:
            max_price = round(price * (1 - tolerance), 4)

        slice_count = _slice_count(raw.notional, tier)

        out.append(replace(
            raw,
            tier=tier,
            decision_price=price,
            max_price=max_price,
            slice_count=slice_count,
        ))
    return out


def _tier_for(symbol: str, ctx: PricingContext) -> str:
    if symbol in config.DEFENSIVE_SYMBOLS:
        return "HIGH"
    rank = ctx.ranks.get(symbol, 99)
    return "HIGH" if rank == 1 else "MED"


def _slice_count(notional: float, tier: str) -> int:
    if notional < config.PLANNER_DIRECT_SUBMIT_THRESHOLD:
        return 1
    bucket = "small" if notional < config.SLICE_SIZE_SMALL_MAX else "large"
    return config.SLICE_COUNTS[tier][bucket]


class RuleBasedIntentPricer:
    """Rule-based tier assignment + max_price + slice_count.

    Wraps the existing build_priced_intents() — behavior identical.
    Implements planning.IntentPricer."""

    def price(self, intents, ctx: PricingContext):
        from planning import IntentPricerOutput
        priced = build_priced_intents(intents, ctx)
        return IntentPricerOutput(priced=priced, provider="rule-based")
