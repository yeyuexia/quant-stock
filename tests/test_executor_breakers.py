# tests/test_executor_breakers.py
import datetime as dt
from quant.execution.pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
from quant.execution.orders import OrderIntent
from tests.fakes import FakeBroker


def _plan_with_intents(intents):
    return PendingPlan(
        plan_id="core-2026-04-17", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                          news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc)),
        intents=intents,
    )


def _intent(symbol, side="buy"):
    return OrderIntent(
        symbol=symbol, notional=1000.0, side=side,
        reason="test", tranche="core", client_order_id=f"cid-{symbol}-{side}",
        tier="MED", decision_price=100.0, max_price=101.0, slice_count=2,
    )


def test_breaker_a_aborts_all_buys_not_sells(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    plan = _plan_with_intents([
        IntentState(intent=_intent("SPY", "buy")),
        IntentState(intent=_intent("XLE", "sell")),
    ])
    write_plan(plan)

    class Obs:
        spy = 470.0              # −2.08% → trips A
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 470.0, "XLE": 90.0}
        spy_15min_ago = 470.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    by_side = {s.intent.side: s for s in loaded.intents}
    assert by_side["buy"].status == "aborted"
    assert "A" in (by_side["buy"].abort_reason or "")
    assert by_side["sell"].status == "active"
    assert "A" in loaded.breakers_tripped


def test_breaker_e_spares_defensive_buys(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    plan = _plan_with_intents([
        IntentState(intent=_intent("SPY", "buy")),
        IntentState(intent=_intent("BIL", "buy")),
    ])
    write_plan(plan)

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = -0.15                # baseline 0.20 → drop 0.35 trips E
        symbol_prices = {"SPY": 480.0, "BIL": 100.0}
        spy_15min_ago = 480.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    by_sym = {s.intent.symbol: s for s in loaded.intents}
    assert by_sym["SPY"].status == "aborted"
    assert by_sym["BIL"].status == "active"
    assert "E" in loaded.breakers_tripped


def test_breaker_c_aborts_only_one_symbol(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    plan = _plan_with_intents([
        IntentState(intent=_intent("NVDA", "buy")),
        IntentState(intent=_intent("SPY", "buy")),
    ])
    write_plan(plan)

    class Obs:
        spy = 478.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"NVDA": 94.0, "SPY": 478.0}   # NVDA decision was 100 → −6%
        spy_15min_ago = 478.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    by_sym = {s.intent.symbol: s for s in loaded.intents}
    assert by_sym["NVDA"].status == "aborted"
    assert by_sym["SPY"].status == "active"


def test_sticky_breaker_stays_tripped_on_next_tick(tmp_path, monkeypatch):
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    plan = _plan_with_intents([IntentState(intent=_intent("SPY", "buy"))])
    write_plan(plan)

    class ObsTrip:
        spy = 470.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 470.0}
        spy_15min_ago = 470.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: ObsTrip())
    executor.run_tick(broker=FakeBroker())

    class ObsRecover:
        spy = 485.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 485.0}
        spy_15min_ago = 485.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: ObsRecover())
    executor.run_tick(broker=FakeBroker())

    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert loaded.breakers_tripped == ["A"]


def test_per_symbol_sticky_allows_new_c_trips_on_different_symbols(tmp_path, monkeypatch):
    """Breaker C on NVDA should not silence a subsequent C on a different symbol."""
    import quant.execution.executor as executor, quant.execution.orders as orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))

    plan = _plan_with_intents([
        IntentState(intent=_intent("NVDA", "buy")),
        IntentState(intent=_intent("AMD", "buy")),
        IntentState(intent=_intent("TSLA", "buy")),
    ])
    write_plan(plan)

    # Tick 1: NVDA crashes -6%
    class Obs1:
        spy = 478.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"NVDA": 94.0, "AMD": 100.0, "TSLA": 100.0}
        spy_15min_ago = 478.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs1())
    r1 = executor.run_tick(broker=FakeBroker())
    assert any(br.breaker == "C" and (br.affected_symbols or [None])[0] == "NVDA"
               for br in r1.tripped_breakers)

    # Tick 2: AMD crashes -6% (TSLA stable). NVDA already aborted but new C event on AMD
    class Obs2:
        spy = 478.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"NVDA": 94.0, "AMD": 93.0, "TSLA": 100.0}
        spy_15min_ago = 478.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs2())
    r2 = executor.run_tick(broker=FakeBroker())

    # AMD must get its own notification; current code silently drops it.
    assert any(br.breaker == "C" and (br.affected_symbols or [None])[0] == "AMD"
               for br in r2.tripped_breakers), \
        "New C trip on AMD was swallowed by sticky 'C' filter"

    # Both NVDA and AMD are aborted now
    loaded = load_plan()
    by_sym = {s.intent.symbol: s for s in loaded.intents}
    assert by_sym["NVDA"].status == "aborted"
    assert by_sym["AMD"].status == "aborted"
    assert by_sym["TSLA"].status == "active"

    # breakers_tripped should now have per-symbol entries for C
    assert "C:NVDA" in loaded.breakers_tripped
    assert "C:AMD" in loaded.breakers_tripped
