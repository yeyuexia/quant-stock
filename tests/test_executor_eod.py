# tests/test_executor_eod.py
import datetime as dt
from quant.execution.pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
from quant.execution.orders import OrderIntent
from tests.fakes import FakeBroker


def _plan(intent_kwargs):
    return PendingPlan(
        plan_id="p", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc)),
        intents=[IntentState(**intent_kwargs)],
    )


def test_eod_marks_unfilled_deferred(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 15, 50))

    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="t", tranche="core", client_order_id="cid-spy",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )
    plan = _plan(dict(intent=intent, slices_submitted=2,
                      notional_filled=250.0, last_client_order_id="cid-spy-s3"))
    write_plan(plan)

    from quant.execution.broker import Order
    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.9, ask=480.1)
    b.seed_open_order(Order(id="ord-pending", symbol="SPY", side="buy",
                            type="limit", qty=None, notional=250.0,
                            status="accepted", client_order_id="cid-spy-s3",
                            parent_order_id=None))

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = 0.0
        symbol_prices = {"SPY": 480.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: Obs())

    result = executor.run_tick(broker=b)

    loaded = load_plan()
    assert "ord-pending" in b._canceled
    assert loaded.intents[0].status == "deferred"


def test_eod_marks_nearly_filled_done(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 15, 50))

    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="t", tranche="core", client_order_id="cid-spy",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )
    plan = _plan(dict(intent=intent, slices_submitted=4,
                      notional_filled=970.0, last_client_order_id=None))
    write_plan(plan)

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = 0.0
        symbol_prices = {"SPY": 480.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    assert load_plan().intents[0].status == "done"
