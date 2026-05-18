"""Unit tests for sepa_exits — pure-compute SEPA decision helpers."""
import math
import pandas as pd
import pytest

from sepa_exits import (
    initial_r, r_multiple, next_r_tier_action,
    ma_break, ma_trail_should_exit,
)


def _pos(**overrides):
    """Build a position dict matching portfolio.json schema."""
    base = {
        "symbol": "AAPL",
        "shares": 30,
        "avg_entry": 100.0,
        "market_value": 3000.0,
        "tranche": "core",
        "initial_entry_price": 100.0,
        "initial_qty": 30,
        "initial_stop_price": 92.0,
        "r_tier_filled": [],
    }
    base.update(overrides)
    return base


# ── initial_r ──────────────────────────────────────────────────

def test_initial_r_basic():
    assert initial_r(_pos()) == 8.0  # 100 - 92


def test_initial_r_missing_initial_entry_returns_none():
    assert initial_r(_pos(initial_entry_price=None)) is None


def test_initial_r_missing_initial_stop_returns_none():
    assert initial_r(_pos(initial_stop_price=None)) is None


def test_initial_r_zero_when_stop_equals_entry():
    assert initial_r(_pos(initial_stop_price=100.0)) == 0.0


# ── r_multiple ─────────────────────────────────────────────────

def test_r_multiple_at_2r():
    assert math.isclose(r_multiple(_pos(), current_price=116.0), 2.0)


def test_r_multiple_below_entry_is_negative():
    assert math.isclose(r_multiple(_pos(), current_price=96.0), -0.5)


def test_r_multiple_unknown_initial_returns_none():
    assert r_multiple(_pos(initial_entry_price=None), current_price=116.0) is None


def test_r_multiple_zero_r_returns_none():
    """When R==0 (stop == entry), R-multiple is undefined."""
    assert r_multiple(_pos(initial_stop_price=100.0), current_price=120.0) is None


# ── next_r_tier_action ─────────────────────────────────────────

def test_next_r_tier_action_2r_reached_empty_filled():
    assert next_r_tier_action(_pos(), current_price=116.0) == "2R"


def test_next_r_tier_action_3r_reached_with_2r_filled():
    p = _pos(r_tier_filled=["2R"])
    assert next_r_tier_action(p, current_price=124.0) == "3R"


def test_next_r_tier_action_below_2r_returns_none():
    assert next_r_tier_action(_pos(), current_price=110.0) is None


def test_next_r_tier_action_all_filled_returns_none():
    p = _pos(r_tier_filled=["2R", "3R"])
    assert next_r_tier_action(p, current_price=200.0) is None


def test_next_r_tier_action_3r_reached_but_2r_not_filled_returns_2r():
    """Gap-up: position never observed at 2/3 qty yet, so SEPA only triggers 2R first."""
    p = _pos(r_tier_filled=[])
    assert next_r_tier_action(p, current_price=130.0) == "2R"


def test_next_r_tier_action_no_initial_stop_returns_none():
    p = _pos(initial_stop_price=None)
    assert next_r_tier_action(p, current_price=200.0) is None


# ── ma_break ──────────────────────────────────────────────────

def _closes_with_last(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_ma_break_close_below_ema_true():
    # 22 bars rising to 110, then last bar drops to 100.
    vals = list(range(89, 111)) + [100.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="ema") is True


def test_ma_break_close_above_ema_false():
    # 22 bars steady at 100, then last bar at 105.
    vals = [100.0] * 22 + [105.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="ema") is False


def test_ma_break_insufficient_data_returns_none():
    s = _closes_with_last([100.0] * 10)
    assert ma_break(s, period=21, ma_type="ema") is None


def test_ma_break_sma_variant():
    # SMA path also exercised.
    vals = [100.0] * 22 + [50.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="sma") is True


# ── ma_trail_should_exit ───────────────────────────────────────

def test_ma_trail_gated_by_final_tier():
    """Without 3R in r_tier_filled, even a clear MA break returns False."""
    p = _pos(r_tier_filled=["2R"])
    s = _closes_with_last(list(range(89, 111)) + [50.0])
    assert ma_trail_should_exit(p, s) is False


def test_ma_trail_triggers_after_final_tier_when_break():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last(list(range(89, 111)) + [50.0])
    assert ma_trail_should_exit(p, s) is True


def test_ma_trail_no_trigger_when_close_above_ema():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last([100.0] * 22 + [120.0])
    assert ma_trail_should_exit(p, s) is False


def test_ma_trail_insufficient_data_returns_false():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last([100.0] * 10)
    assert ma_trail_should_exit(p, s) is False


# ── failed_breakout ──────────────────────────────────────────────

import datetime as dt


def _closes_with_dates(values, start="2026-05-15"):
    idx = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_failed_breakout_within_window_close_below_pivot_true():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    # entry day = Mon 2026-05-15; Day 0 close=201, Day 1 close=199 (below)
    closes = _closes_with_dates([201.0, 199.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18),  # Mon of week 2
                           window_days=3) is True


def test_failed_breakout_within_window_all_closes_above_pivot_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    closes = _closes_with_dates([201.0, 202.0, 205.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 19), window_days=3) is False


def test_failed_breakout_window_expired_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-11"}}
    # 4 bars after entry (window=3) — past the window
    closes = _closes_with_dates(
        [201.0, 202.0, 203.0, 204.0, 195.0],   # Day 4 close < pivot
        start="2026-05-11",
    )
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False


def test_failed_breakout_no_pivot_record_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {}  # no pivot for AAPL
    closes = _closes_with_dates([180.0, 170.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False


def test_failed_breakout_insufficient_closes_false():
    """Closes series doesn't reach today → no in-window data → False."""
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    closes = pd.Series(dtype=float)  # empty
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False


# ── climax_check ─────────────────────────────────────────────────

def _ohlcv_df(symbol, *, close, high=None, low=None, volume=None, start="2026-01-01"):
    """Build a MultiIndex OHLCV frame matching data.fetch_ohlcv shape."""
    n = len(close)
    idx = pd.date_range(start, periods=n, freq="B")
    if high is None:
        high = [c + 0.5 for c in close]
    if low is None:
        low = [c - 0.5 for c in close]
    if volume is None:
        volume = [1_000_000] * n
    df = pd.DataFrame({
        ("High",   symbol): high,
        ("Low",    symbol): low,
        ("Close",  symbol): close,
        ("Volume", symbol): volume,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_climax_all_three_conditions_true():
    """30% return over 8 days + 3× ADR + 4× volume → climax True."""
    from sepa_exits import climax_check
    # 50 quiet bars (close=100, narrow range, low volume), then 8 wild bars.
    quiet_closes = [100.0] * 50
    quiet_highs  = [100.5] * 50
    quiet_lows   = [99.5] * 50
    quiet_volume = [1_000_000] * 50

    wild_closes  = [102, 105, 108, 112, 116, 121, 126, 130.0]
    wild_highs   = [c + 3 for c in wild_closes]   # ~3× the quiet 1-pt range
    wild_lows    = [c - 3 for c in wild_closes]
    wild_volume  = [4_000_000] * 8                  # 4× the baseline 1M

    df = _ohlcv_df(
        "X",
        close=quiet_closes + wild_closes,
        high=quiet_highs + wild_highs,
        low=quiet_lows + wild_lows,
        volume=quiet_volume + wild_volume,
    )
    assert climax_check(
        df,
        return_lookback=8, return_threshold=0.25,
        range_lookback=20, range_multiplier=2.0,
        volume_lookback=20, volume_multiplier=2.0,
        volume_recent_days=3,
    ) is True


def test_climax_return_only_false():
    """Return high, but range and volume baseline → no climax."""
    from sepa_exits import climax_check
    closes = [100.0] * 50 + [102, 105, 108, 112, 116, 121, 126, 130.0]
    df = _ohlcv_df("X", close=closes)  # default narrow range, flat volume
    assert climax_check(df) is False


def test_climax_range_only_false():
    """Range expanded but return is small."""
    from sepa_exits import climax_check
    # close stays near 100 but daily range widens
    quiet_close = [100.0] * 50
    recent_close = [100.0, 100.5, 99.8, 100.2, 100.6, 100.1, 99.9, 100.3]
    quiet_high = [100.5] * 50
    recent_high = [c + 3 for c in recent_close]
    quiet_low  = [99.5] * 50
    recent_low = [c - 3 for c in recent_close]
    df = _ohlcv_df(
        "X",
        close=quiet_close + recent_close,
        high=quiet_high + recent_high,
        low=quiet_low + recent_low,
    )
    assert climax_check(df) is False


def test_climax_volume_only_false():
    """Volume spiked, but return and range are normal."""
    from sepa_exits import climax_check
    closes = [100.0] * 50 + [100.5, 99.8, 100.2, 100.6, 100.1, 99.9, 100.3, 100.4]
    volume = [1_000_000] * 50 + [4_000_000] * 8
    df = _ohlcv_df("X", close=closes, volume=volume)
    assert climax_check(df) is False


def test_climax_insufficient_data_false():
    """Fewer than 30 bars → not enough history → False."""
    from sepa_exits import climax_check
    df = _ohlcv_df("X", close=[100.0] * 20)
    assert climax_check(df) is False
