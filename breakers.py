"""Circuit breakers that evaluate market state against plan baselines.

Each breaker is a pure function: takes a Baseline + current observations,
returns a BreakerResult. evaluate_all() (added later) orchestrates all five.
Sticky state (which breakers have tripped this plan) lives in PendingPlan,
not here.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import config
from pending_plan import Baseline


@dataclass(frozen=True)
class BreakerResult:
    breaker: str
    tripped: bool
    scope: str
    message: str
    affected_symbols: Optional[list[str]] = None
    measurement: Optional[float] = None


def check_spy_drop(baseline: Baseline, spy_now: float) -> BreakerResult:
    change = (spy_now - baseline.spy) / baseline.spy
    threshold = -config.CIRCUIT_BREAKERS["spy_drop_pct"]
    if change <= threshold:
        return BreakerResult(
            breaker="A",
            tripped=True,
            scope="buys",
            message=f"SPY dropped {change * 100:.2f}% from baseline "
                    f"{baseline.spy:.2f} (threshold {threshold * 100:.2f}%)",
            measurement=change,
        )
    return BreakerResult(
        breaker="A", tripped=False, scope="none",
        message=f"SPY {change * 100:+.2f}% vs baseline (ok)",
        measurement=change,
    )


def check_vix_spike(baseline: Baseline, vix_now: float) -> BreakerResult:
    cb = config.CIRCUIT_BREAKERS
    threshold = max(baseline.vix * cb["vix_multiplier"], cb["vix_absolute"])
    if vix_now >= threshold:
        return BreakerResult(
            breaker="B",
            tripped=True,
            scope="buys",
            message=f"VIX {vix_now:.2f} ≥ threshold {threshold:.2f} "
                    f"(baseline {baseline.vix:.2f} × {cb['vix_multiplier']}, "
                    f"abs floor {cb['vix_absolute']})",
            measurement=vix_now,
        )
    return BreakerResult(
        breaker="B", tripped=False, scope="none",
        message=f"VIX {vix_now:.2f} below threshold {threshold:.2f}",
        measurement=vix_now,
    )


def check_single_name_shock(
    baseline: Baseline,
    symbol_baselines: dict,
    symbol_prices_now: dict,
) -> list:
    threshold = -config.CIRCUIT_BREAKERS["single_name_drop_pct"]
    results = []
    for sym, base in symbol_baselines.items():
        now = symbol_prices_now.get(sym)
        if now is None or base <= 0:
            continue
        change = (now - base) / base
        if change <= threshold:
            results.append(BreakerResult(
                breaker="C",
                tripped=True,
                scope="symbol",
                affected_symbols=[sym],
                message=f"{sym} dropped {change * 100:.2f}% from baseline "
                        f"{base:.2f} (threshold {threshold * 100:.2f}%)",
                measurement=change,
            ))
        else:
            results.append(BreakerResult(
                breaker="C", tripped=False, scope="none",
                affected_symbols=[sym],
                message=f"{sym} {change * 100:+.2f}% vs baseline (ok)",
                measurement=change,
            ))
    return results
