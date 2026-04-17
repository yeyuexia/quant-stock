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
