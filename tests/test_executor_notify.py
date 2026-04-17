# tests/test_executor_notify.py
import json
import datetime as dt
from pending_plan import PendingPlan, IntentState, Baseline, write_plan
from orders import OrderIntent
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
    import executor, orders, config as cfg
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
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
