# tests/test_executor_skeleton.py
import datetime as dt
from pending_plan import PendingPlan, IntentState, Baseline, write_plan, clear_plan
from orders import OrderIntent
from tests.fakes import FakeBroker


def _plan():
    return PendingPlan(
        plan_id="core-2026-04-17",
        tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(
            spy=480.0, vix=14.0, macro_score=0.12,
            news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        ),
        intents=[
            IntentState(intent=OrderIntent(
                symbol="SPY", notional=1000.0, side="buy",
                reason="core rebalance", tranche="core",
                client_order_id="cid-spy",
                tier="MED", decision_price=480.0, max_price=481.44, slice_count=2,
            )),
        ],
    )


def test_executor_exits_when_no_plan(tmp_path, monkeypatch):
    import executor
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "none.json"))
    ret = executor.run_tick(broker=FakeBroker())
    assert ret is None


def test_executor_respects_halt(tmp_path, monkeypatch):
    import executor, orders
    halt = tmp_path / "HALT"
    halt.write_text("")
    monkeypatch.setattr(orders, "HALT_PATH", str(halt))
    monkeypatch.setattr(executor, "HALT_PATH", str(halt))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    write_plan(_plan())
    b = FakeBroker()
    result = executor.run_tick(broker=b)
    assert result is not None
    assert result.halted is True
    assert len(result.submitted) == 0


def test_executor_respects_market_closed(tmp_path, monkeypatch):
    import executor, orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    write_plan(_plan())
    b = FakeBroker(market_open=False)
    result = executor.run_tick(broker=b)
    assert result is not None
    assert result.market_closed is True


def test_executor_shadow_mode_logs_without_submitting(tmp_path, monkeypatch):
    import executor, orders, config as cfg
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", True)
    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.9, ask=480.1)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda plan, broker: _FakeObs(spy=480.0, vix=14.0,
                                                     macro=0.12,
                                                     symbol_prices={"SPY": 480.0},
                                                     spy_15min_ago=480.0,
                                                     news_hits=[]))
    write_plan(_plan())
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 30))
    result = executor.run_tick(broker=b)
    assert result.shadow is True
    # Shadow mode => no real submissions on the broker
    assert len(b._submitted) == 0


class _FakeObs:
    def __init__(self, spy, vix, macro, symbol_prices, spy_15min_ago, news_hits):
        self.spy = spy
        self.vix = vix
        self.macro = macro
        self.symbol_prices = symbol_prices
        self.spy_15min_ago = spy_15min_ago
        self.news_hits = news_hits
