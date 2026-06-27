# tests/test_executor_integration.py
"""Opt-in integration test: runs a small plan through the real Alpaca paper account.

Requires ALPACA_API_KEY + ALPACA_API_SECRET in the environment. Run with:
  ALPACA_API_KEY=... ALPACA_API_SECRET=... python3 -m pytest -m integration \\
    tests/test_executor_integration.py -v
"""
import os
import time
import pytest
import datetime as dt

integration = pytest.mark.integration


@integration
def test_submit_limit_on_paper_roundtrips():
    from quant.execution.broker import Broker
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET")):
        pytest.skip("Alpaca credentials not set")
    b = Broker(env="paper")
    if not b.is_market_open():
        pytest.skip("Market closed — skipping integration test")

    # Buy $100 of BIL at ~market price, then cancel.
    bid, ask = b.latest_quote("BIL")
    cid = f"test-integ-{int(time.time())}"
    o = b.submit_limit("BIL", notional=100.0, side="buy",
                       limit_price=round(ask * 1.001, 2),
                       client_order_id=cid)
    assert o.symbol == "BIL"
    assert o.type == "limit"
    time.sleep(2)
    b.cancel_order(o.id)


@integration
def test_executor_runs_against_paper_with_shadow_mode():
    """Smoke: write a minimal plan, run one executor tick in shadow mode."""
    from quant.execution.broker import Broker
    from quant.execution.pending_plan import PendingPlan, IntentState, Baseline, write_plan, clear_plan
    from quant.execution.orders import OrderIntent
    import quant.execution.executor as executor, quant.config as cfg
    if not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_API_SECRET")):
        pytest.skip("Alpaca credentials not set")
    b = Broker(env="paper")
    if not b.is_market_open():
        pytest.skip("Market closed")

    original_shadow = cfg.EXECUTOR_SHADOW_MODE
    cfg.EXECUTOR_SHADOW_MODE = True
    try:
        baseline = Baseline(spy=480.0, vix=14.0, macro_score=0.12,
                            news_cursor_at=dt.datetime.now(dt.timezone.utc))
        plan = PendingPlan(
            plan_id="integ-shadow",
            tranche="core",
            created_at=dt.datetime.now(dt.timezone.utc),
            baseline=baseline,
            intents=[IntentState(intent=OrderIntent(
                symbol="BIL", notional=100.0, side="buy",
                reason="integ-test", tranche="core",
                client_order_id=f"integ-{int(time.time())}",
                tier="HIGH", decision_price=91.0,
                max_price=91.5, slice_count=1,
            ))],
        )
        write_plan(plan)
        result = executor.run_tick(broker=b)
        assert result is not None
        assert result.shadow is True
    finally:
        clear_plan()
        cfg.EXECUTOR_SHADOW_MODE = original_shadow
