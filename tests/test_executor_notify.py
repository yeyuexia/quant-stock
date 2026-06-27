# tests/test_executor_notify.py
import json
import datetime as dt
from quant.execution.pending_plan import PendingPlan, IntentState, Baseline, write_plan
from quant.execution.orders import OrderIntent
from tests.fakes import FakeBroker


def _plan():
    return PendingPlan(
        plan_id="p", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                          news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="SPY", notional=1000.0, side="buy",
            reason="t", tranche="core", client_order_id="cid",
            tier="MED", decision_price=480.0, max_price=481.5, slice_count=2,
        ))],
    )


def test_breaker_trip_writes_notification_file(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders, quant.config as cfg
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    write_plan(_plan())

    class Obs:
        spy = 470.0     # −2% → trips A
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 470.0}
        spy_15min_ago = 470.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())

    assert notify_path.exists()
    notifications = json.loads(notify_path.read_text())
    assert any("A" in n.get("breaker", "") for n in notifications)


def _plan_with_prior_slice(cid_prior: str):
    """Plan with one intent that has an outstanding prior slice (last_client_order_id set),
    meaning the next tick will query its filled notional and detect any fills."""
    return PendingPlan(
        plan_id="p", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                          news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc)),
        intents=[IntentState(
            intent=OrderIntent(
                symbol="SPY", notional=10_000.0, side="buy",
                reason="t", tranche="core", client_order_id="cid-spy",
                tier="MED", decision_price=480.0, max_price=481.5, slice_count=2,
            ),
            slices_submitted=1,
            last_client_order_id=cid_prior,
        )],
    )


def test_slice_fill_writes_notification(tmp_path, monkeypatch):
    """When _process_slices detects a non-zero prior_filled (= a slice filled),
    one TG notification per filled intent is appended, with source='executor-fill'."""
    import quant.execution.executor as executor, quant.execution.orders as orders, quant.config as cfg
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    write_plan(_plan_with_prior_slice("cid-spy-s1"))

    broker = FakeBroker()
    broker.set_latest_quote("SPY", bid=479.95, ask=480.05)
    broker.set_fill("cid-spy-s1", 3_000.0)  # prior slice filled $3k

    class Obs:
        spy = 480.0; vix = 14.0; macro = 0.20
        symbol_prices = {"SPY": 480.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=broker)

    assert notify_path.exists()
    notifs = json.loads(notify_path.read_text())
    fills = [n for n in notifs if n.get("source") == "executor-fill"]
    assert len(fills) == 1, f"expected 1 fill notification, got {len(fills)}"
    msg = fills[0]["message"]
    assert "SPY" in msg
    assert "BUY" in msg or "buy" in msg.lower()
    assert "3,000" in msg or "3000" in msg


def test_zero_fill_writes_no_notification(tmp_path, monkeypatch):
    """If the prior slice filled zero, no fill notification should be written."""
    import quant.execution.executor as executor, quant.execution.orders as orders, quant.config as cfg
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    write_plan(_plan_with_prior_slice("cid-spy-s1"))

    broker = FakeBroker()
    broker.set_latest_quote("SPY", bid=479.95, ask=480.05)
    # no set_fill → get_filled_notional returns 0.0

    class Obs:
        spy = 480.0; vix = 14.0; macro = 0.20
        symbol_prices = {"SPY": 480.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=broker)

    if notify_path.exists():
        notifs = json.loads(notify_path.read_text())
        fills = [n for n in notifs if n.get("source") == "executor-fill"]
        assert fills == []
