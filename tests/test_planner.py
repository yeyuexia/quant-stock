# tests/test_planner.py
from orders import OrderIntent
from planner import build_priced_intents, PricingContext


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
