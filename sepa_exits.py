"""Mark Minervini SEPA exit rules — Phase 1 (R-multiple scale-out + EMA trail).

All functions are pure: side-effect free, no I/O, no broker access. Callers
fetch the data and feed it in.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

import config


def _tier_label(r_multiple_value: float) -> str:
    """Stable label derived from the R multiple, e.g. 2.0 → '2R'."""
    return f"{int(r_multiple_value)}R"


def initial_r(position: dict) -> Optional[float]:
    """R per share = initial_entry_price − initial_stop_price.

    Returns None if either initial field is missing.
    """
    entry = position.get("initial_entry_price")
    stop = position.get("initial_stop_price")
    if entry is None or stop is None:
        return None
    return float(entry) - float(stop)


def r_multiple(position: dict, current_price: float) -> Optional[float]:
    """(current_price − initial_entry_price) / R. None if R undefined or zero."""
    r = initial_r(position)
    if r is None or r == 0:
        return None
    entry = float(position["initial_entry_price"])
    return (float(current_price) - entry) / r


def next_r_tier_action(position: dict, current_price: float) -> Optional[str]:
    """Return the label of the next R-tier to action, or None.

    Iterates config.SEPA_R_TIERS in order; returns the first tier whose
    R-multiple has been reached AND whose label is not already in
    r_tier_filled. Returns None if no tier qualifies or R is undefined.
    """
    rm = r_multiple(position, current_price)
    if rm is None:
        return None
    filled = position.get("r_tier_filled") or []
    for r, _frac in config.SEPA_R_TIERS:
        label = _tier_label(r)
        if label in filled:
            continue
        if rm >= r:
            return label
        return None
    return None


def _final_tier_label() -> str:
    """Label of the last entry in SEPA_R_TIERS."""
    r, _ = config.SEPA_R_TIERS[-1]
    return _tier_label(r)


def ma_break(closes: pd.Series, period: int = 21, ma_type: str = "ema") -> Optional[bool]:
    """True if the most recent close < MA(period). None on insufficient data.

    `ma_type` "ema" uses pandas .ewm(span=period, adjust=False); "sma" uses
    rolling mean. period+1 bars required.
    """
    s = closes.dropna()
    if len(s) < period + 1:
        return None
    if ma_type == "ema":
        ma = s.ewm(span=period, adjust=False).mean().iloc[-1]
    elif ma_type == "sma":
        ma = s.rolling(period).mean().iloc[-1]
    else:
        raise ValueError(f"unknown ma_type: {ma_type!r}")
    return float(s.iloc[-1]) < float(ma)


def ma_trail_should_exit(position: dict, closes: pd.Series) -> bool:
    """True only when r_tier_filled contains the final tier AND ma_break is True.

    Returns False (not None) when gating conditions aren't met — this is the
    "do nothing" signal, distinct from data-unavailable (also False here).
    """
    filled = position.get("r_tier_filled") or []
    if _final_tier_label() not in filled:
        return False
    broke = ma_break(closes, period=config.SEPA_MA_PERIOD, ma_type=config.SEPA_MA_TYPE)
    return broke is True
