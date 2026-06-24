# tests/test_executor_e2e.py
"""Simulated trading day: plan → ticks 10:00…15:50 → verify end state."""
import datetime as dt
from tests.fakes import FakeBroker
from pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
from orders import OrderIntent


def _run_day(monkeypatch, tmp_path, *, obs_by_hour, shadow=False):
    import executor, orders, config as cfg
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", shadow)
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(tmp_path / "notif.json"))

    b = FakeBroker()
    b.set_latest_quote("SPY", bid=479.95, ask=480.05)

    plan = PendingPlan(
        plan_id="core-2026-04-17",
        tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.12,
                          news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="SPY", notional=1000.0, side="buy",
            reason="rebalance", tranche="core", client_order_id="cid-spy",
            tier="MED", decision_price=480.0, max_price=481.44, slice_count=2,
        ))],
    )
    write_plan(plan)

    tick_hours = [(10, 0), (10, 30), (11, 0), (12, 0), (13, 0),
                  (14, 0), (14, 30), (15, 0), (15, 50)]
    for h, m in tick_hours:
        now = dt.datetime(2026, 4, 17, h, m)
        monkeypatch.setattr(executor, "_now_et", lambda n=now: n)
        obs_factory = obs_by_hour.get((h, m), obs_by_hour[(99, 99)])
        monkeypatch.setattr(executor, "_fetch_current_observations",
                            lambda p, br, _obs=obs_factory: _obs())
        executor.run_tick(broker=b)
    return b


def _quiet_obs():
    class O:
        spy = 480.0; vix = 14.0; macro = 0.12
        symbol_prices = {"SPY": 480.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    return O()


def test_full_day_quiet_market_submits_both_slices(tmp_path, monkeypatch):
    obs_by_hour = {(99, 99): _quiet_obs}
    b = _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    state = loaded.intents[0]
    # Both slices submitted (2-slice plan, 10:30 + 14:30 windows)
    assert state.slices_submitted == 2
    # FakeBroker doesn't fill orders; notional_filled stays 0 → EOD marks deferred
    assert state.status in ("deferred", "done")
    assert len(b._submitted) == 2


def test_full_day_breaker_trip_aborts_after_morning(tmp_path, monkeypatch):
    """At 11:00, SPY crashes −2%. First slice (10:30) already submitted;
    remaining aborted."""
    def crash_obs():
        class O:
            spy = 470.0
            vix = 14.0; macro = 0.12
            symbol_prices = {"SPY": 470.0}
            spy_15min_ago = 470.0
            news_hits: list = []
        return O()

    obs_by_hour = {
        (10, 0): _quiet_obs,
        (10, 30): _quiet_obs,
        (11, 0): crash_obs,
        (99, 99): crash_obs,
    }
    _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    state = loaded.intents[0]
    assert state.status == "aborted"
    assert "A" in loaded.breakers_tripped


def test_full_day_shadow_mode_submits_nothing(tmp_path, monkeypatch):
    obs_by_hour = {(99, 99): _quiet_obs}
    b = _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour, shadow=True)
    assert len(b._submitted) == 0


# ── Per-breaker contract tests (one full day per breaker) ──────────
# Mirror test_full_day_breaker_trip_aborts_after_morning for B/C/D/E so
# each circuit breaker has at least one full-path e2e regression.


def _obs_with(*, spy=480.0, vix=14.0, macro=0.12, spy_15min_ago=480.0,
              symbol_prices=None, news_hits=None):
    """Factory helper — builds the duck-typed observations object the
    executor expects (see _fetch_current_observations)."""
    sp = symbol_prices if symbol_prices is not None else {"SPY": spy}
    nh = news_hits if news_hits is not None else []
    class O:
        pass
    O.spy = spy
    O.vix = vix
    O.macro = macro
    O.symbol_prices = sp
    O.spy_15min_ago = spy_15min_ago
    O.news_hits = nh
    return lambda: O()


def test_full_day_breaker_B_vix_spike(tmp_path, monkeypatch):
    """VIX spikes past max(baseline×1.5, 25.0) at 11:00 → buys abort."""
    obs_by_hour = {
        (10, 0):  _quiet_obs,
        (10, 30): _quiet_obs,
        # baseline VIX 14 × 1.5 = 21, abs floor 25 — pick 30 to clear both
        (11, 0):  _obs_with(vix=30.0),
        (99, 99): _obs_with(vix=30.0),
    }
    _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert "B" in loaded.breakers_tripped


def test_full_day_breaker_C_single_name_drop(tmp_path, monkeypatch):
    """SPY (the plan's only symbol) drops 6% from decision_price at 11:00,
    but SPY benchmark stays flat — only C trips, not A. The intent is
    aborted with key C:SPY (per-symbol scope)."""
    obs_by_hour = {
        (10, 0):  _quiet_obs,
        (10, 30): _quiet_obs,
        # spy benchmark stays at baseline so A doesn't fire; per-symbol
        # price for SPY drops 6%, tripping C (threshold 5%).
        (11, 0):  _obs_with(symbol_prices={"SPY": 451.0}),
        (99, 99): _obs_with(symbol_prices={"SPY": 451.0}),
    }
    _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert "C:SPY" in loaded.breakers_tripped


def test_full_day_breaker_D_news_shock_corroborated(tmp_path, monkeypatch):
    """News hit + SPY moves > 0.5% in the 15-min window → D trips."""
    from news_shock import NewsHit

    def news_obs():
        hit = NewsHit(
            title="Fed announces emergency rate cut",
            source="reuters",
            ts=dt.datetime(2026, 4, 17, 14, 55, tzinfo=dt.timezone.utc),
            matched="rate cut",
        )
        return _obs_with(
            spy=477.0, spy_15min_ago=480.0,   # -0.625% — won't fire A (need 1.5%)
            news_hits=[hit],                   # but D fires: corroboration only needs 0.5%
        )()

    obs_by_hour = {
        (10, 0):  _quiet_obs,
        (10, 30): _quiet_obs,
        (11, 0):  lambda: news_obs(),
        (99, 99): lambda: news_obs(),
    }
    _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert "D" in loaded.breakers_tripped


def test_full_day_breaker_E_macro_flip(tmp_path, monkeypatch):
    """Macro score drops 0.3+ from baseline at 11:00 → E trips."""
    obs_by_hour = {
        (10, 0):  _quiet_obs,
        (10, 30): _quiet_obs,
        # baseline macro 0.12, drop to -0.20 → drop = 0.32 (≥ 0.30 threshold)
        (11, 0):  _obs_with(macro=-0.20),
        (99, 99): _obs_with(macro=-0.20),
    }
    _run_day(monkeypatch, tmp_path, obs_by_hour=obs_by_hour)

    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert "E" in loaded.breakers_tripped
