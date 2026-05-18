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
    """True when ma_break is True AND the position has reached the MA-trail
    backstop phase — either by completing the final R-tier (Phase 1) OR by
    having fired climax (Phase 2: climax_fired=True).

    Returns False (not None) when gating conditions aren't met — this is the
    "do nothing" signal, distinct from data-unavailable (also False here).
    """
    filled = position.get("r_tier_filled") or []
    if _final_tier_label() not in filled and not position.get("climax_fired"):
        return False
    broke = ma_break(closes, period=config.SEPA_MA_PERIOD, ma_type=config.SEPA_MA_TYPE)
    return broke is True


import datetime as _dt


def failed_breakout(position: dict, pivots: dict, closes: pd.Series,
                    *, today: _dt.date,
                    window_days: int = 3) -> bool:
    """Phase 2 — Minervini 3-day failed-breakout rule.

    True iff:
      - `pivots[position['symbol']]` exists with a `pivot` and `entry_date`
      - the count of `closes` index dates strictly after entry_date and
        ≤ today is between 1 and window_days inclusive
      - at least one of those in-window closes is below `pivot`

    The window-day count is observed bars (handles weekends/holidays
    naturally). Returns False on any missing data.
    """
    symbol = position.get("symbol")
    rec = pivots.get(symbol) if symbol else None
    if rec is None:
        return False
    pivot = float(rec["pivot"])
    entry_date = _dt.date.fromisoformat(rec["entry_date"])
    if closes is None or closes.empty:
        return False

    # In-window bars: strictly after entry_date and on/before today.
    idx = closes.index
    in_window = closes[(idx.date > entry_date) & (idx.date <= today)]
    if in_window.empty or len(in_window) > window_days:
        return False
    return bool((in_window < pivot).any())


def climax_check(ohlcv: pd.DataFrame, *,
                 return_lookback: int = 8,
                 return_threshold: float = 0.25,
                 range_lookback: int = 20,
                 range_multiplier: float = 2.0,
                 volume_lookback: int = 20,
                 volume_multiplier: float = 2.0,
                 volume_recent_days: int = 3) -> bool:
    """Phase 2 — Minervini climax / blow-off detection.

    Returns True iff all three conditions hold:
      1. Cumulative return over `return_lookback` bars ≥ `return_threshold`.
      2. Mean daily range over the LAST `range_lookback` bars
         ≥ `range_multiplier` × mean daily range over the PRIOR `range_lookback`
         bars (i.e. bars [-2L:-L]).
      3. Max volume over the LAST `volume_recent_days` bars
         ≥ `volume_multiplier` × mean volume over the prior `volume_lookback`
         bars EXCLUDING those recent days
         (i.e. bars [-volume_lookback − volume_recent_days : -volume_recent_days]).

    `ohlcv` is the MultiIndex frame returned by `data.fetch_ohlcv`. The single
    ticker is selected from the column index automatically. Returns False on
    insufficient data.
    """
    if ohlcv is None or ohlcv.empty:
        return False
    # Single-ticker selection from the MultiIndex.
    try:
        close = ohlcv["Close"].iloc[:, 0].dropna()
        high  = ohlcv["High"].iloc[:, 0].dropna()
        low   = ohlcv["Low"].iloc[:, 0].dropna()
        volume = ohlcv["Volume"].iloc[:, 0].dropna()
    except (KeyError, IndexError):
        return False

    needed = max(return_lookback + 1,
                 2 * range_lookback,
                 volume_lookback + volume_recent_days)
    if len(close) < needed:
        return False

    # 1. Return
    ret = (float(close.iloc[-1]) / float(close.iloc[-return_lookback - 1])) - 1.0
    if ret < return_threshold:
        return False

    # 2. Range expansion
    daily_range = (high - low).dropna()
    if len(daily_range) < 2 * range_lookback:
        return False
    recent_range = daily_range.iloc[-range_lookback:].mean()
    prior_range  = daily_range.iloc[-2 * range_lookback:-range_lookback].mean()
    if not (prior_range > 0):
        return False
    if recent_range < range_multiplier * prior_range:
        return False

    # 3. Volume spike — baseline EXCLUDES the recent days under test.
    if len(volume) < volume_lookback + volume_recent_days:
        return False
    recent_vol = volume.iloc[-volume_recent_days:].max()
    baseline_vol = volume.iloc[
        -volume_lookback - volume_recent_days : -volume_recent_days
    ].mean()
    if not (baseline_vol > 0):
        return False
    if recent_vol < volume_multiplier * baseline_vol:
        return False

    return True
