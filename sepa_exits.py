"""Mark Minervini SEPA exit rules — Phase 1 (R-multiple scale-out + EMA trail)
+ Phase 2 (failed-breakout + climax detection).

All functions are pure: side-effect free, no I/O, no broker access. Callers
fetch the data and feed it in.
"""
from __future__ import annotations
import datetime as _dt
import logging as _logging
from typing import Optional
import pandas as pd

import config

_log = _logging.getLogger(__name__)


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
    """(current_price − initial_entry_price) / R. None if R undefined or ≤ 0.

    Why R ≤ 0 returns None (was: only R == 0):
      Normal long: entry > stop → R > 0. R ≤ 0 means stop was set at or
      above entry — corrupt metadata (stop wasn't backfilled correctly,
      manual edit gone wrong, etc.). Continuing the computation would
      give a flipped-sign R-multiple that silently mis-fires the SEPA
      scale-out logic. Returning None forces the caller to skip the
      tier check for this position and (ideally) surface an alert.
    """
    r = initial_r(position)
    if r is None or r <= 0:
        return None
    entry = float(position["initial_entry_price"])
    return (float(current_price) - entry) / r


def next_r_tier_action(position: dict, current_price: float) -> Optional[str]:
    """Return the label of the next R-tier to action, or None.

    Iterates config.SEPA_R_TIERS in order; returns the first tier whose
    R-multiple has been reached AND whose label is not already in
    r_tier_filled. Returns None if no tier qualifies or R is undefined.

    Assumes SEPA_R_TIERS is monotonically ascending in R-multiple. This is
    asserted at config load time (see config.py), but the loop bails on
    the first unfilled-but-not-reached tier under that assumption — if
    you ever pass a non-monotonic list directly, results are undefined.
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


def ma_break(closes: pd.Series, period: int = 21,
             ma_type: str = "ema") -> Optional[bool]:
    """True if the most recent close < MA(period). None on insufficient
    data OR unknown ma_type.

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
        # Defensive: was a ValueError raise; changed to None to match the
        # rest of sepa_exits' "missing → no action" convention. Callers
        # shouldn't crash because a config knob got typo'd.
        _log.warning("ma_break: unknown ma_type %r, returning None", ma_type)
        return None
    return float(s.iloc[-1]) < float(ma)


def ma_trail_should_exit(position: dict, closes: pd.Series) -> bool:
    """True when ma_break is True AND the position has reached the MA-trail
    backstop phase — either by completing the final R-tier (Phase 1) OR by
    having fired climax (Phase 2: climax_fired=True).

    Returns False (not None) when gating conditions aren't met — this is the
    "do nothing" signal, distinct from data-unavailable (also False here).
    When the position IS in the backstop phase but `closes` doesn't have
    enough history for the MA, a warning is logged so the silent gap is
    observable in production.
    """
    filled = position.get("r_tier_filled") or []
    in_backstop_phase = (
        _final_tier_label() in filled or position.get("climax_fired")
    )
    if not in_backstop_phase:
        return False
    broke = ma_break(closes, period=config.SEPA_MA_PERIOD,
                     ma_type=config.SEPA_MA_TYPE)
    if broke is None:
        _log.warning(
            "ma_trail_should_exit: %s in MA-backstop phase but closes "
            "history insufficient for MA(%d) — exit check skipped",
            position.get("symbol", "?"), config.SEPA_MA_PERIOD,
        )
        return False
    return broke is True


def failed_breakout(position: dict, pivots: dict, closes: pd.Series,
                    *, today: _dt.date,
                    window_days: Optional[int] = None) -> bool:
    """Phase 2 — Minervini 3-day failed-breakout rule.

    True iff:
      - `pivots[position['symbol']]` exists with both `pivot` (float) and
        `entry_date` (ISO date string)
      - the count of `closes` index dates strictly after entry_date and
        ≤ today is between 1 and window_days inclusive
      - at least one of those in-window closes is below `pivot`

    `today` MUST be the current UTC date (typically `dt.datetime.now(tz=utc).date()`).
    Passing a stale date silently excludes today's close from the window —
    you'd miss a same-day failed breakout.

    `window_days` defaults to `config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS` so
    callers don't accidentally drift from the configured policy.

    The window-day count is observed bars (handles weekends/holidays
    naturally). Returns False on any missing/malformed data — corruption
    in entry_pivots.json never crashes the SEPA chain.
    """
    if window_days is None:
        window_days = config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS

    symbol = position.get("symbol")
    rec = pivots.get(symbol) if symbol else None
    if rec is None:
        return False

    pivot_raw = rec.get("pivot")
    entry_date_raw = rec.get("entry_date")
    if pivot_raw is None or entry_date_raw is None:
        _log.warning("failed_breakout: %s pivot record missing fields: %r",
                     symbol, rec)
        return False

    try:
        pivot = float(pivot_raw)
    except (TypeError, ValueError):
        _log.warning("failed_breakout: %s pivot not numeric: %r", symbol, pivot_raw)
        return False

    try:
        entry_date = _dt.date.fromisoformat(str(entry_date_raw))
    except (TypeError, ValueError):
        _log.warning("failed_breakout: %s entry_date unparseable: %r",
                     symbol, entry_date_raw)
        return False

    if closes is None or closes.empty:
        return False

    # In-window bars: strictly after entry_date and on/before today.
    idx = closes.index
    in_window = closes[(idx.date > entry_date) & (idx.date <= today)]
    if in_window.empty or len(in_window) > window_days:
        return False
    return bool((in_window < pivot).any())


def climax_check(ohlcv: pd.DataFrame, symbol: str, *,
                 return_lookback: Optional[int] = None,
                 return_threshold: Optional[float] = None,
                 range_lookback: Optional[int] = None,
                 range_multiplier: Optional[float] = None,
                 volume_lookback: Optional[int] = None,
                 volume_multiplier: Optional[float] = None,
                 volume_recent_days: Optional[int] = None) -> bool:
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

    `ohlcv` is the MultiIndex frame returned by `data.fetch_ohlcv` — may
    contain multiple tickers. `symbol` selects the column explicitly
    (caller MUST pass the right symbol; was previously `iloc[:, 0]` which
    picked alphabetically-first column and silently mis-fired on multi-
    ticker frames).

    Default tuning constants fall back to `config.SEPA_CLIMAX_*` when not
    explicitly passed, so callers can't drift from the policy by re-stating
    stale defaults inline.

    Returns False on insufficient data, missing symbol, or any per-bar
    NaN that prevents the OHLCV streams from aligning.
    """
    # Fill defaults from config — single source of truth.
    if return_lookback   is None: return_lookback   = config.SEPA_CLIMAX_RETURN_LOOKBACK
    if return_threshold  is None: return_threshold  = config.SEPA_CLIMAX_RETURN_THRESHOLD
    if range_lookback    is None: range_lookback    = config.SEPA_CLIMAX_RANGE_LOOKBACK
    if range_multiplier  is None: range_multiplier  = config.SEPA_CLIMAX_RANGE_MULTIPLIER
    if volume_lookback   is None: volume_lookback   = config.SEPA_CLIMAX_VOLUME_LOOKBACK
    if volume_multiplier is None: volume_multiplier = config.SEPA_CLIMAX_VOLUME_MULTIPLIER
    if volume_recent_days is None: volume_recent_days = config.SEPA_CLIMAX_VOLUME_RECENT_DAYS

    if ohlcv is None or ohlcv.empty:
        return False
    # Single-ticker selection with explicit symbol.
    try:
        if symbol in ohlcv["Close"].columns:
            close = ohlcv["Close"][symbol]
            high  = ohlcv["High"][symbol]
            low   = ohlcv["Low"][symbol]
            volume = ohlcv["Volume"][symbol]
        else:
            # Backward compat: single-ticker frame (no MultiIndex level for
            # symbols, just the field). Picks the lone column.
            close = ohlcv["Close"].iloc[:, 0]
            high  = ohlcv["High"].iloc[:, 0]
            low   = ohlcv["Low"].iloc[:, 0]
            volume = ohlcv["Volume"].iloc[:, 0]
    except (KeyError, IndexError):
        return False

    # Align all four series via a shared mask so dropna() drops on one
    # doesn't desync the slice indices (previously: high/low NaN would
    # shrink daily_range below close, and .iloc[-20:] addressed different
    # calendar bars per series).
    common = ~(close.isna() | high.isna() | low.isna() | volume.isna())
    close  = close[common]
    high   = high[common]
    low    = low[common]
    volume = volume[common]

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
    daily_range = high - low
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
