# tests/test_pending_plan.py
import datetime as dt
from pending_plan import (
    PendingPlan, IntentState, Baseline, write_plan, load_plan, clear_plan,
)
from orders import OrderIntent


def _sample_intent(symbol="SPY", notional=1000.0):
    return OrderIntent(
        symbol=symbol, notional=notional, side="buy",
        reason="test", tranche="core",
        client_order_id=f"cid-{symbol}",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )


def test_write_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    baseline = Baseline(
        spy=480.0, vix=14.0, macro_score=0.12,
        news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
    )
    plan = PendingPlan(
        plan_id="core-2026-04-17",
        tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=baseline,
        intents=[IntentState(intent=_sample_intent())],
    )
    write_plan(plan)
    loaded = load_plan()
    assert loaded is not None
    assert loaded.plan_id == "core-2026-04-17"
    assert loaded.baseline.spy == 480.0
    assert loaded.intents[0].intent.symbol == "SPY"
    assert loaded.intents[0].status == "active"


def test_load_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "missing.json"))
    assert load_plan() is None


def test_clear_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    baseline = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                        news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    plan = PendingPlan(plan_id="t", tranche="core", created_at=baseline.news_cursor_at,
                       baseline=baseline, intents=[])
    write_plan(plan)
    clear_plan()
    assert load_plan() is None


def test_intent_state_defaults():
    s = IntentState(intent=_sample_intent())
    assert s.status == "active"
    assert s.notional_filled == 0.0
    assert s.slices_submitted == 0
    assert s.last_client_order_id is None
    assert s.abort_reason is None
