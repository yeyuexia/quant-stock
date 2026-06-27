"""Two-track selection rules + scoring over Fundamentals. Pure — no I/O.
Track A = profitable 'mispriced bargains'; Track B = unprofitable growth.
Thresholds come from config.VS_TRACK_A / VS_TRACK_B so they're tunable."""
import config
from value_fundamentals import Fundamentals


def classify(f: Fundamentals):
    if f.market_cap is None or f.market_cap < config.VS_MIN_MARKET_CAP:
        return None
    return "A" if f.is_profitable else "B"


def _runway_q(f: Fundamentals):
    """Cash runway in quarters. None if uncomputable; inf if cash-flow positive."""
    if f.fcf is None or f.total_cash is None:
        return None
    burn = max(0.0, -f.fcf) / 4.0
    return float("inf") if burn == 0 else f.total_cash / burn


def _lt(v, th):  # fail-open: missing value does not reject on this gate
    return v is None or v < th
def _gt(v, th):
    return v is None or v > th


def passes(f: Fundamentals, track: str) -> bool:
    if track == "A":
        c = config.VS_TRACK_A
        cheap = any(x is not None for x in (f.peg, f.pe))
        growth = any(x is not None for x in (f.rev_growth, f.eps_growth, f.gross_margin))
        solv = any(x is not None for x in (f.debt_equity, f.current_ratio, f.fcf))
        ok = (_lt(f.peg, c["peg_max"]) and _lt(f.pe, c["pe_max"])
              and _gt(f.rev_growth, c["rev_growth_min"]) and _gt(f.eps_growth, c["eps_growth_min"])
              and _gt(f.gross_margin, c["gross_margin_min"])
              and _lt(f.debt_equity, c["debt_equity_max"]) and _gt(f.current_ratio, c["current_ratio_min"])
              and (f.fcf is None or f.fcf > 0))
        return ok and cheap and growth and solv
    c = config.VS_TRACK_B
    cheap = f.ps is not None
    growth = any(x is not None for x in (f.rev_growth, f.gross_margin))
    runway = _runway_q(f)
    solv = (f.debt_equity is not None) or (runway is not None)
    ok = (_lt(f.ps, c["ps_max"]) and _gt(f.rev_growth, c["rev_growth_min"])
          and _gt(f.gross_margin, c["gross_margin_min"]) and _lt(f.debt_equity, c["debt_equity_max"])
          and (runway is None or runway > c["cash_runway_quarters_min"]))
    return ok and cheap and growth and solv


def score(f: Fundamentals, track: str) -> float:
    parts = []
    if track == "A":
        if f.pe and f.pe > 0: parts.append(min(1.0 / f.pe, 0.2) * 5)
        if f.peg is not None: parts.append(max(0.0, 1.5 - f.peg))
        if f.rev_growth is not None: parts.append(min(f.rev_growth, 1.0))
        if f.gross_margin is not None: parts.append(f.gross_margin)
    else:
        if f.ps and f.ps > 0: parts.append(max(0.0, (6.0 - f.ps) / 6.0))
        if f.rev_growth is not None: parts.append(min(f.rev_growth, 2.0) / 2.0)
        if f.gross_margin is not None: parts.append(f.gross_margin)
        r = _runway_q(f)
        if r is not None and r != float("inf"): parts.append(min(r / 12.0, 1.0))
    return round(sum(parts) / len(parts), 4) if parts else 0.0
