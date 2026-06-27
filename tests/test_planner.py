# tests/test_planner.py
from quant.execution.orders import OrderIntent
from quant.execution.planner import build_priced_intents, PricingContext
from quant.execution.planner import build_priced_intents, PricingContext, _tier_for, _slice_count
import quant.config as config
import os
import pytest
import sys


def _intent(symbol, notional, side="buy"):
    return OrderIntent(
        symbol=symbol, notional=notional, side=side,
        reason="core rebalance", tranche="core",
        client_order_id=f"cid-{symbol}",
        stop_pct=0.08, trail_pct=0.12,
    )


def test_top1_etf_gets_high_tier_50bps_tolerance():
    ctx = PricingContext(
        ranks={"SPY": 1, "QQQ": 2},
        asset_class={"SPY": "etf", "QQQ": "etf"},
        decision_prices={"SPY": 480.0, "QQQ": 400.0},
        tranche="core",
    )
    intents = build_priced_intents([_intent("SPY", 1000), _intent("QQQ", 1000)], ctx)
    spy, qqq = intents
    assert spy.tier == "HIGH"
    assert round(spy.max_price, 2) == round(480.0 * 1.005, 2)
    assert qqq.tier == "MED"
    assert round(qqq.max_price, 2) == round(400.0 * 1.003, 2)


def test_stock_tolerance_wider():
    ctx = PricingContext(
        ranks={"AAPL": 1},
        asset_class={"AAPL": "stock"},
        decision_prices={"AAPL": 180.0},
        tranche="core",
    )
    [i] = build_priced_intents([_intent("AAPL", 1500)], ctx)
    assert i.tier == "HIGH"
    assert round(i.max_price, 2) == round(180.0 * 1.010, 2)


def test_aggressive_tranche_multiplier():
    ctx = PricingContext(
        ranks={"TQQQ": 1},
        asset_class={"TQQQ": "etf"},
        decision_prices={"TQQQ": 60.0},
        tranche="aggressive",
    )
    [i] = build_priced_intents([_intent("TQQQ", 3000)], ctx)
    # HIGH etf = 50 bps, × 1.5 aggressive = 75 bps
    assert round(i.max_price, 2) == round(60.0 * (1 + 0.005 * 1.5), 2)


def test_defensive_gets_high_tier_regardless_of_rank():
    ctx = PricingContext(
        ranks={"BIL": 5},
        asset_class={"BIL": "etf"},
        decision_prices={"BIL": 91.0},
        tranche="core",
    )
    [i] = build_priced_intents([_intent("BIL", 2000)], ctx)
    assert i.tier == "HIGH"


def test_slice_count_small_vs_large():
    ctx = PricingContext(
        ranks={"SPY": 1, "QQQ": 2, "IWM": 3},
        asset_class={s: "etf" for s in ("SPY", "QQQ", "IWM")},
        decision_prices={"SPY": 480.0, "QQQ": 400.0, "IWM": 200.0},
        tranche="core",
    )
    intents = build_priced_intents([
        _intent("SPY", 1000),
        _intent("QQQ", 5000),
        _intent("IWM", 5000),
    ], ctx)
    [spy, qqq, iwm] = intents
    assert spy.slice_count == 2
    assert qqq.slice_count == 4
    assert iwm.slice_count == 4


def test_sell_side_uses_min_price_floor():
    ctx = PricingContext(
        ranks={"XLE": 2},
        asset_class={"XLE": "etf"},
        decision_prices={"XLE": 90.0},
        tranche="core",
    )
    sell = _intent("XLE", 1500, side="sell")
    [i] = build_priced_intents([sell], ctx)
    # sells: max_price stores the FLOOR (decision × (1 - tol))
    assert i.tier == "MED"
    assert round(i.max_price, 2) == round(90.0 * (1 - 0.003), 2)


def test_unpriced_intent_passes_through_unchanged_when_price_is_zero():
    ctx = PricingContext(
        ranks={"XYZ": 1},
        asset_class={"XYZ": "etf"},
        decision_prices={"XYZ": 0.0},
        tranche="core",
    )
    [i] = build_priced_intents([_intent("XYZ", 1000)], ctx)
    assert i.tier is None
    assert i.max_price is None
    assert i.decision_price is None
    assert i.slice_count is None


def test_unpriced_intent_passes_through_when_price_is_negative():
    ctx = PricingContext(
        ranks={"XYZ": 1},
        asset_class={"XYZ": "etf"},
        decision_prices={"XYZ": -5.0},  # nonsense value
        tranche="core",
    )
    [i] = build_priced_intents([_intent("XYZ", 1000)], ctx)
    assert i.tier is None


# ======================================================================
# Post-review additions (formerly test_planner_optimizations.py)
# ======================================================================

"""Regression tests for planner.py hardening (PL1 + PL3 defensive fallbacks)."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from quant.execution.orders import OrderIntent
from quant.execution.planner import build_priced_intents, PricingContext, _tier_for, _slice_count
import quant.config as config


def _intent_opt(symbol="SPY", side="buy", notional=1000.0):
    return OrderIntent(
        symbol=symbol, notional=notional, side=side,
        reason="t", tranche="core",
        client_order_id=f"cid-{symbol}-{side}",
    )


def _ctx(prices=None, ranks=None, asset_class=None, tranche="core"):
    return PricingContext(
        ranks=ranks or {"SPY": 1, "QQQ": 2},
        asset_class=asset_class or {"SPY": "etf", "QQQ": "etf"},
        decision_prices=prices or {"SPY": 480.0, "QQQ": 400.0},
        tranche=tranche,
    )


def test_corrupt_side_returns_intent_unchanged():
    """Unknown side ("hold", "") used to fall through to sell branch and
    compute a wrong-side max_price."""
    bad = _intent_opt(side="hold")
    out = build_priced_intents([bad], _ctx())
    assert len(out) == 1
    # max_price not set because we refused to enrich
    assert out[0].max_price is None
    assert out[0].tier is None


def test_unknown_asset_class_falls_back_to_med_stock():
    """asset_class with typo should not crash — fall back to MED stock bps."""
    ctx = PricingContext(
        ranks={"WEIRD": 1},
        asset_class={"WEIRD": "crypto"},   # not "etf" or "stock"
        decision_prices={"WEIRD": 100.0},
        tranche="core",
    )
    out = build_priced_intents([_intent_opt("WEIRD")], ctx)
    assert len(out) == 1
    assert out[0].max_price is not None
    assert out[0].tier == "HIGH"   # rank 1


def test_defensive_symbol_gets_high_tier():
    """DEFENSIVE_SYMBOLS always HIGH tier regardless of rank."""
    bil_intent = _intent_opt("BIL", side="buy", notional=2000.0)
    ctx = _ctx(
        prices={"BIL": 100.0},
        ranks={"BIL": 99},  # would normally be MED
        asset_class={"BIL": "etf"},
    )
    out = build_priced_intents([bil_intent], ctx)
    assert out[0].tier == "HIGH"


def test_slice_count_tiny_intent_is_one():
    """notional < PLANNER_DIRECT_SUBMIT_THRESHOLD → slice_count=1."""
    assert _slice_count(100.0, "MED") == 1
    assert _slice_count(499.0, "HIGH") == 1


def test_slice_count_respects_tier_buckets():
    """Above threshold uses SLICE_COUNTS dispatch table."""
    small = config.SLICE_SIZE_SMALL_MAX - 100.0
    large = config.SLICE_SIZE_SMALL_MAX + 100.0
    assert _slice_count(small, "MED") == config.SLICE_COUNTS["MED"]["small"]
    assert _slice_count(large, "MED") == config.SLICE_COUNTS["MED"]["large"]


def test_missing_price_returns_intent_unchanged():
    """No decision_price for a symbol → enrich passes through."""
    ctx = PricingContext(
        ranks={"SPY": 1}, asset_class={"SPY": "etf"},
        decision_prices={},  # SPY missing
        tranche="core",
    )
    out = build_priced_intents([_intent_opt("SPY")], ctx)
    assert out[0].max_price is None


def test_aggressive_tranche_widens_tolerance():
    """Aggressive multiplier should produce wider max_price than core."""
    intent = _intent_opt("SPY", side="buy", notional=5000.0)
    core_out = build_priced_intents([intent], _ctx(tranche="core"))
    agg_out = build_priced_intents([intent], _ctx(tranche="aggressive"))
    # Both buys → max_price > decision_price; aggressive max > core max
    assert agg_out[0].max_price > core_out[0].max_price
