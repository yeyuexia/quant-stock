# tests/test_executor_submission.py
import datetime as dt
from pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
from orders import OrderIntent
from tests.fakes import FakeBroker


def _plan(intent, *, slices_submitted=0, last_cid=None):
    return PendingPlan(
        plan_id="p", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc)),
        intents=[IntentState(
            intent=intent, slices_submitted=slices_submitted,
            last_client_order_id=last_cid,
        )],
    )


def _base_intent(max_price=481.5, slice_count=2, notional=1000.0):
    return OrderIntent(
        symbol="SPY", notional=notional, side="buy",
        reason="test", tranche="core", client_order_id="cid-spy",
        tier="MED", decision_price=480.0, max_price=max_price,
        slice_count=slice_count,
    )


class _Obs:
    def __init__(self, **kw):
        self.spy = kw.get("spy", 480.0)
        self.vix = kw.get("vix", 14.0)
        self.macro = kw.get("macro", 0.0)
        self.symbol_prices = kw.get("symbol_prices", {"SPY": 480.0})
        self.spy_15min_ago = kw.get("spy_15min_ago", 480.0)
        self.news_hits = kw.get("news_hits", [])


def _setup(tmp_path, monkeypatch, now_et=(11, 0), shadow=False):
    import executor, orders, config as cfg
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", shadow)
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, *now_et))


def test_submits_slice_when_window_passed(tmp_path, monkeypatch):
    import executor
    _setup(tmp_path, monkeypatch, now_et=(11, 0))
    write_plan(_plan(_base_intent()))
    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.95, ask=480.05)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: _Obs(symbol_prices={"SPY": 480.0}))
    result = executor.run_tick(broker=b)
    assert len(result.submitted) == 1
    loaded = load_plan()
    assert loaded.intents[0].slices_submitted == 1
    assert loaded.intents[0].last_client_order_id is not None


def test_skips_slice_when_ask_above_max_price(tmp_path, monkeypatch):
    import executor
    _setup(tmp_path, monkeypatch, now_et=(11, 0))
    write_plan(_plan(_base_intent(max_price=481.0)))
    b = FakeBroker()
    # Ask 482 exceeds max_price 481
    b.set_latest_quote("SPY", bid=481.9, ask=482.1)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: _Obs(symbol_prices={"SPY": 482.0}))
    result = executor.run_tick(broker=b)
    assert len(result.submitted) == 0
    loaded = load_plan()
    assert loaded.intents[0].slices_submitted == 0
    assert loaded.intents[0].status == "active"
    assert any("max_price" in n.lower() or "ceiling" in n.lower()
               for n in result.notes)


def test_cancels_prior_unfilled_before_new_slice(tmp_path, monkeypatch):
    import executor
    from broker import Order
    _setup(tmp_path, monkeypatch, now_et=(14, 30))
    intent = _base_intent(slice_count=2)
    plan = _plan(intent, slices_submitted=1, last_cid="prior-cid")
    write_plan(plan)
    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.95, ask=480.05)
    prior = Order(id="ord-prior", symbol="SPY", side="buy", type="limit",
                  qty=None, notional=250.0, status="accepted",
                  client_order_id="prior-cid", parent_order_id=None)
    b.seed_open_order(prior)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: _Obs(symbol_prices={"SPY": 480.0}))
    result = executor.run_tick(broker=b)
    assert "ord-prior" in b._canceled
    assert len(result.submitted) == 1


def test_shadow_mode_does_not_submit(tmp_path, monkeypatch):
    import executor
    _setup(tmp_path, monkeypatch, now_et=(11, 0), shadow=True)
    write_plan(_plan(_base_intent()))
    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.95, ask=480.05)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: _Obs(symbol_prices={"SPY": 480.0}))
    result = executor.run_tick(broker=b)
    assert result.shadow is True
    assert len(result.submitted) == 0
    assert len(result.would_submit) == 1
    assert result.would_submit[0]["symbol"] == "SPY"


def test_sell_side_uses_min_price_floor(tmp_path, monkeypatch):
    import executor
    _setup(tmp_path, monkeypatch, now_et=(11, 0))
    sell = OrderIntent(
        symbol="XLE", notional=1500.0, side="sell",
        reason="rebalance-sell", tranche="core", client_order_id="cid-xle",
        tier="MED", decision_price=90.0, max_price=89.73, slice_count=2,
    )
    write_plan(_plan(sell))
    b = FakeBroker()
    b.set_latest_quote("XLE", bid=89.50, ask=89.60)
    monkeypatch.setattr(executor, "_fetch_current_observations",
                        lambda p, b: _Obs(symbol_prices={"XLE": 89.55}))
    result = executor.run_tick(broker=b)
    assert len(result.submitted) == 0

    b2 = FakeBroker()
    b2.set_latest_quote("XLE", bid=89.80, ask=89.90)
    write_plan(_plan(sell))
    result = executor.run_tick(broker=b2)
    assert len(result.submitted) == 1
