"""Regression tests for executor.py optimizations (E1, E5, E6, E10, E11).

These cover behaviors that the original test suite didn't pin down because the
optimizations are about what happens BETWEEN ticks (sticky state, cross-tick
dedup, skip-write idempotency)."""
import datetime as dt
import json
import os
import pytest
from unittest.mock import MagicMock, patch

from pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
from orders import OrderIntent
from broker import BrokerError
from tests.fakes import FakeBroker


# ── helpers ────────────────────────────────────────────────────────

def _intent(symbol, side="buy", cid_suffix=""):
    return OrderIntent(
        symbol=symbol, notional=1000.0, side=side,
        reason="test", tranche="core",
        client_order_id=f"cid-{symbol}-{side}{cid_suffix}",
        tier="MED", decision_price=100.0, max_price=101.0, slice_count=2,
    )


def _plan_with_intents(intents, *, breakers_tripped=None, news_hits_seen=None):
    return PendingPlan(
        plan_id="core-2026-04-17", tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                          news_cursor_at=dt.datetime(2026, 4, 17, 13, 35,
                                                     tzinfo=dt.timezone.utc)),
        intents=intents,
        breakers_tripped=breakers_tripped or [],
        news_hits_seen=news_hits_seen or {},
    )


def _setup_paths(tmp_path, monkeypatch):
    import executor, orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    # Place us inside RTH so _is_rth_now() passes.
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 11, 0))
    monkeypatch.setattr(executor, "_is_rth_now", lambda: True)


# ── E5: sticky abort re-application ────────────────────────────────

def test_sticky_breaker_a_aborts_mid_day_merged_intent(tmp_path, monkeypatch):
    """Plan has A already tripped from earlier; a NEW active intent (mimicking
    a mid-day manual rebalance merge) must be aborted by the sticky re-apply."""
    import executor
    _setup_paths(tmp_path, monkeypatch)

    plan = _plan_with_intents(
        [IntentState(intent=_intent("LATE_ADD", "buy"))],
        breakers_tripped=["A"],   # tripped earlier in the day
    )
    write_plan(plan)

    class Obs:
        spy = 478.0   # ok now, but A was sticky
        vix = 14.0
        macro = 0.20
        symbol_prices = {"LATE_ADD": 100.0}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    assert loaded.intents[0].status == "aborted"
    assert "A: sticky" in (loaded.intents[0].abort_reason or "")


def test_sticky_breaker_e_exempts_defensive_symbols(tmp_path, monkeypatch):
    """E (macro flip) sticky abort must still exempt DEFENSIVE_SYMBOLS."""
    import executor, config
    _setup_paths(tmp_path, monkeypatch)
    defensive = next(iter(config.DEFENSIVE_SYMBOLS))

    plan = _plan_with_intents([
        IntentState(intent=_intent("QQQ", "buy")),
        IntentState(intent=_intent(defensive, "buy")),
    ], breakers_tripped=["E"])
    write_plan(plan)

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"QQQ": 400.0, defensive: 99.0}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    by_sym = {s.intent.symbol: s for s in loaded.intents}
    assert by_sym["QQQ"].status == "aborted"
    assert by_sym[defensive].status == "active"


def test_sticky_breaker_c_aborts_only_matching_symbol(tmp_path, monkeypatch):
    """C is per-symbol — sticky 'C:NVDA' should abort NVDA only, not AAPL."""
    import executor
    _setup_paths(tmp_path, monkeypatch)

    plan = _plan_with_intents([
        IntentState(intent=_intent("NVDA", "buy")),
        IntentState(intent=_intent("AAPL", "buy")),
    ], breakers_tripped=["C:NVDA"])
    write_plan(plan)

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = 0.20
        symbol_prices = {"NVDA": 150.0, "AAPL": 175.0}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    executor.run_tick(broker=FakeBroker())
    loaded = load_plan()
    by_sym = {s.intent.symbol: s for s in loaded.intents}
    assert by_sym["NVDA"].status == "aborted"
    assert by_sym["AAPL"].status == "active"


# ── E1: skip eval for already-tripped breakers ─────────────────────

def test_already_tripped_breaker_a_not_re_added(tmp_path, monkeypatch):
    """A was tripped on a prior tick; this tick must not re-add it to
    tripped_breakers (would duplicate the TG notification)."""
    import executor
    _setup_paths(tmp_path, monkeypatch)

    plan = _plan_with_intents(
        [IntentState(intent=_intent("SPY", "buy"))],
        breakers_tripped=["A"],
    )
    write_plan(plan)

    class Obs:
        spy = 460.0   # still below threshold — would re-trip A if eval'd
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 460.0}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    result = executor.run_tick(broker=FakeBroker())
    # A was sticky — should NOT appear in this tick's tripped_breakers
    assert not any(r.breaker == "A" for r in result.tripped_breakers)


# ── E6: news dedupe across ticks ───────────────────────────────────

def test_news_dedupe_persists_across_ticks(tmp_path, monkeypatch):
    """A headline already in plan.news_hits_seen on a prior tick should NOT
    appear in the new hits list on this tick (and so not be re-logged)."""
    import executor, news_shock
    _setup_paths(tmp_path, monkeypatch)

    title = "Fed surprise rate cut shocks markets"
    h = news_shock.title_hash(title)

    plan = _plan_with_intents(
        [IntentState(intent=_intent("SPY", "buy"))],
        news_hits_seen={h: dt.datetime.now(dt.timezone.utc).isoformat()},
    )
    write_plan(plan)

    fake_hit = news_shock.NewsHit(
        title=title, source="yahoo",
        ts=dt.datetime.now(dt.timezone.utc), matched="rate cut",
    )

    # Stub fetch_recent_headlines + match_headlines via direct monkeypatch.
    monkeypatch.setattr(news_shock, "fetch_recent_headlines",
                        lambda since: [{"title": title, "source": "yahoo",
                                        "ts": dt.datetime.now(dt.timezone.utc)}])
    monkeypatch.setattr(news_shock, "match_headlines",
                        lambda heads, kws, syms: [fake_hit])
    monkeypatch.setattr(news_shock, "dedupe_by_title_hash",
                        lambda hits, win: hits)

    # Patch the baseline fetchers + 15min so the obs path runs.
    monkeypatch.setattr("baseline._fetch_spy", lambda: 480.0)
    monkeypatch.setattr("baseline._fetch_vix", lambda: 14.0)
    monkeypatch.setattr("baseline._fetch_macro_score", lambda: 0.20)
    monkeypatch.setattr(executor, "_spy_15min_ago_price", lambda: 480.0)

    fb = FakeBroker()
    fb.set_latest_quote("SPY", 100.0, 100.1)

    executor.run_tick(broker=fb)
    loaded = load_plan()
    # Same hash still present (with possibly updated ts), no duplicate entries
    assert h in loaded.news_hits_seen
    assert len(loaded.news_hits_seen) == 1
    # D should NOT have tripped — hits got filtered to empty by cross-tick dedupe
    assert "D" not in loaded.breakers_tripped


def test_news_dedupe_admits_genuinely_new_hit(tmp_path, monkeypatch):
    """A different headline (different hash) must be admitted and stamped."""
    import executor, news_shock
    _setup_paths(tmp_path, monkeypatch)

    old_title = "Old news from yesterday"
    new_title = "Fresh breaking news today"
    old_h = news_shock.title_hash(old_title)

    plan = _plan_with_intents(
        [IntentState(intent=_intent("SPY", "buy"))],
        news_hits_seen={old_h: dt.datetime.now(dt.timezone.utc).isoformat()},
    )
    write_plan(plan)

    new_hit = news_shock.NewsHit(
        title=new_title, source="yahoo",
        ts=dt.datetime.now(dt.timezone.utc), matched="breaking",
    )
    monkeypatch.setattr(news_shock, "fetch_recent_headlines",
                        lambda since: [{"title": new_title, "source": "yahoo",
                                        "ts": dt.datetime.now(dt.timezone.utc)}])
    monkeypatch.setattr(news_shock, "match_headlines",
                        lambda heads, kws, syms: [new_hit])
    monkeypatch.setattr(news_shock, "dedupe_by_title_hash",
                        lambda hits, win: hits)
    monkeypatch.setattr("baseline._fetch_spy", lambda: 480.0)
    monkeypatch.setattr("baseline._fetch_vix", lambda: 14.0)
    monkeypatch.setattr("baseline._fetch_macro_score", lambda: 0.20)
    monkeypatch.setattr(executor, "_spy_15min_ago_price", lambda: 480.0)
    fb = FakeBroker()
    fb.set_latest_quote("SPY", 100.0, 100.1)

    executor.run_tick(broker=fb)
    loaded = load_plan()
    new_h = news_shock.title_hash(new_title)
    assert new_h in loaded.news_hits_seen


# ── E10: skip write_plan on idle ticks ─────────────────────────────

def test_write_plan_skipped_when_nothing_changes(tmp_path, monkeypatch):
    """A tick with no fills, no aborts, no breakers should not rewrite the
    plan file."""
    import executor
    _setup_paths(tmp_path, monkeypatch)

    plan = _plan_with_intents([
        IntentState(intent=_intent("SPY", "buy"),
                    status="done", notional_filled=1000.0),
    ])
    write_plan(plan)
    plan_path = tmp_path / "p.json"
    mtime_before = os.path.getmtime(plan_path)

    class Obs:
        spy = 480.0
        vix = 14.0
        macro = 0.20
        symbol_prices: dict = {}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    import time
    time.sleep(0.05)   # ensure mtime delta would be visible
    executor.run_tick(broker=FakeBroker())
    mtime_after = os.path.getmtime(plan_path)
    assert mtime_before == mtime_after


def test_write_plan_fires_when_breaker_trips(tmp_path, monkeypatch):
    """A new breaker trip mutates plan.breakers_tripped → must write."""
    import executor
    _setup_paths(tmp_path, monkeypatch)

    plan = _plan_with_intents([IntentState(intent=_intent("SPY", "buy"))])
    write_plan(plan)
    plan_path = tmp_path / "p.json"
    mtime_before = os.path.getmtime(plan_path)

    class Obs:
        spy = 460.0   # trips A
        vix = 14.0
        macro = 0.20
        symbol_prices = {"SPY": 460.0}
        spy_15min_ago = 0.0
        news_hits: list = []
    monkeypatch.setattr(executor, "_fetch_current_observations", lambda p, b: Obs())

    import time
    time.sleep(0.05)
    executor.run_tick(broker=FakeBroker())
    mtime_after = os.path.getmtime(plan_path)
    assert mtime_after > mtime_before


# ── E11: broker retry helper ───────────────────────────────────────

def test_retry_broker_succeeds_on_second_attempt():
    import executor
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerError("transient")
        return "ok"
    assert executor._retry_broker(flaky) == "ok"
    assert calls["n"] == 2


def test_retry_broker_raises_after_all_attempts():
    import executor
    def always_fail():
        raise BrokerError("dead")
    with pytest.raises(BrokerError):
        executor._retry_broker(always_fail)


# ── E8: outside RTH skips broker call entirely ────────────────────

def test_outside_rth_skips_broker_call(tmp_path, monkeypatch):
    """Local clock outside 9:30–16:00 ET → market_closed without calling broker."""
    import executor, orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    # 18:00 ET on a weekday — definitely outside RTH
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 17, 18, 0))

    plan = _plan_with_intents([IntentState(intent=_intent("SPY", "buy"))])
    write_plan(plan)

    broker_mock = MagicMock()
    result = executor.run_tick(broker=broker_mock)
    assert result.market_closed is True
    # broker.is_market_open must NOT have been called
    broker_mock.is_market_open.assert_not_called()


def test_weekend_skips_broker_call(tmp_path, monkeypatch):
    import executor, orders
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(executor, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "p.json"))
    # 2026-04-18 is a Saturday
    monkeypatch.setattr(executor, "_now_et",
                        lambda: dt.datetime(2026, 4, 18, 11, 0))

    plan = _plan_with_intents([IntentState(intent=_intent("SPY", "buy"))])
    write_plan(plan)

    broker_mock = MagicMock()
    result = executor.run_tick(broker=broker_mock)
    assert result.market_closed is True
    broker_mock.is_market_open.assert_not_called()


# ── E7: notifications helper (lock-protected append) ───────────────

def test_append_notification_creates_new_file(tmp_path, monkeypatch):
    import notifications, config
    notify_path = tmp_path / "tg.json"
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH", str(notify_path))

    notifications.append_notification({"source": "test", "message": "hello"})
    data = json.loads(notify_path.read_text())
    assert len(data) == 1
    assert data[0]["source"] == "test"
    assert "ts" in data[0]


def test_append_notification_appends_to_existing(tmp_path, monkeypatch):
    import notifications, config
    notify_path = tmp_path / "tg.json"
    notify_path.write_text(json.dumps([{"source": "old", "message": "x", "ts": "2026-01-01"}]))
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH", str(notify_path))

    notifications.append_notification({"source": "new", "message": "y"})
    data = json.loads(notify_path.read_text())
    assert len(data) == 2
    assert data[0]["source"] == "old"
    assert data[1]["source"] == "new"


def test_append_notification_recovers_from_corrupt_file(tmp_path, monkeypatch):
    """A pre-existing corrupted JSON file shouldn't cause loss — start fresh."""
    import notifications, config
    notify_path = tmp_path / "tg.json"
    notify_path.write_text("not valid json {{{")
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH", str(notify_path))

    notifications.append_notification({"source": "fresh", "message": "ok"})
    data = json.loads(notify_path.read_text())
    assert len(data) == 1
    assert data[0]["source"] == "fresh"


def test_append_notification_with_explicit_path_override(tmp_path, monkeypatch):
    import notifications, config
    main_path = tmp_path / "main.json"
    quant_path = tmp_path / "quant.json"
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH", str(main_path))

    notifications.append_notification({"source": "q", "message": "x"},
                                       path=str(quant_path))
    # quant path got the message; main path untouched
    assert json.loads(quant_path.read_text())[0]["source"] == "q"
    assert not main_path.exists()
