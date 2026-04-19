from quant.schema import ProposedChange


def _change(**kwargs):
    defaults = dict(
        key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075,
        rationale="r", detailed_plan="p", expected_effect="e",
        risk_tier="low", confidence=0.7,
    )
    defaults.update(kwargs)
    return ProposedChange(**defaults)


def test_classify_stop_loss_within_band_is_low():
    from quant.applier import classify_change
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    assert classify_change(c) == "low"


def test_classify_stop_loss_out_of_band_is_high():
    from quant.applier import classify_change
    # +30% from 0.08 is 0.104, outside the ±20% band → high-risk
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.104)
    assert classify_change(c) == "high"


def test_classify_stop_loss_out_of_absolute_bounds_is_rejected():
    from quant.applier import classify_change
    # 0.50 is outside the absolute bound [0.04, 0.20]
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.50)
    assert classify_change(c) == "rejected_out_of_bounds"


def test_classify_momentum_top_n_is_always_high():
    from quant.applier import classify_change
    c = _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3)
    assert classify_change(c) == "high"


def test_classify_daily_max_orders_is_forbidden():
    from quant.applier import classify_change
    c = _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100)
    assert classify_change(c) == "forbidden"


def test_classify_watchlist_addition_is_low():
    from quant.applier import classify_change
    current = ["SPY", "QQQ"]
    proposed = current + ["PLTR"]
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "low"


def test_classify_watchlist_removal_is_high():
    from quant.applier import classify_change
    current = ["SPY", "QQQ", "IWM"]
    proposed = ["SPY", "QQQ"]
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "high"


def test_classify_watchlist_over_size_cap_is_rejected():
    from quant.applier import classify_change
    current = [f"T{i}" for i in range(99)]
    proposed = current + ["NEW_ONE", "NEW_TWO"]    # >100
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "rejected_out_of_bounds"
