"""Circuit breakers that evaluate market state against plan baselines.

Each breaker is a pure function: takes a Baseline + current observations,
returns a BreakerResult. Sticky state (which breakers have tripped this
plan) lives in PendingPlan, not here. Audit logging (news hits etc.) is
the caller's responsibility — the breakers themselves never touch disk.
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
    if baseline.spy <= 0:
        return BreakerResult(
            breaker="A", tripped=False, scope="none",
            message=f"invalid baseline SPY {baseline.spy!r}",
        )
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
    if baseline.vix <= 0:
        return BreakerResult(
            breaker="B", tripped=False, scope="none",
            message=f"invalid baseline VIX {baseline.vix!r}",
        )
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


def check_news_shock(
    *,
    baseline: Baseline,
    hits: list,
    spy_now: float,
    spy_15min_ago: float,
) -> BreakerResult:
    """Requires BOTH: at least one matched headline, AND SPY moved > threshold
    in the corroboration window. See news_shock.py for match/dedupe logic."""
    if not hits:
        return BreakerResult(breaker="D", tripped=False, scope="none",
                             message="no news hits")

    if spy_15min_ago <= 0:
        return BreakerResult(breaker="D", tripped=False, scope="none",
                             message="no 15-min-ago SPY reference")

    move = abs(spy_now - spy_15min_ago) / spy_15min_ago
    threshold = config.CIRCUIT_BREAKERS["news_corroboration_pct"]
    tripped = move >= threshold

    # NOTE: audit logging of `hits` is the CALLER's responsibility (executor
    # iterates hits and calls news_shock.log_hit). Keeping this function
    # pure makes it testable without filesystem fixtures and matches the
    # module-level docstring's "no disk" contract.

    if not tripped:
        return BreakerResult(
            breaker="D", tripped=False, scope="none",
            message=f"{len(hits)} news hit(s) but SPY 15min move "
                    f"{move * 100:.2f}% < threshold {threshold * 100:.2f}%",
            measurement=move,
        )

    titles = ", ".join(h.title[:60] for h in hits[:3])
    return BreakerResult(
        breaker="D", tripped=True, scope="buys",
        message=f"news shock corroborated: SPY {move * 100:+.2f}% in 15min; hits: {titles}",
        measurement=move,
    )


def check_macro_flip(baseline: Baseline, macro_now: float) -> BreakerResult:
    threshold = config.CIRCUIT_BREAKERS["macro_drop"]
    drop = baseline.macro_score - macro_now
    if drop >= threshold:
        return BreakerResult(
            breaker="E",
            tripped=True,
            scope="risk_on_buys",
            message=f"macro score dropped from {baseline.macro_score:+.3f} to "
                    f"{macro_now:+.3f} (drop {drop:.3f} ≥ threshold {threshold:.3f})",
            measurement=drop,
        )
    return BreakerResult(
        breaker="E", tripped=False, scope="none",
        message=f"macro score {macro_now:+.3f} vs baseline {baseline.macro_score:+.3f}",
        measurement=drop,
    )
