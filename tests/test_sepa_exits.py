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
