"""Integration tests for watchdog SEPA orchestration against FakeBroker."""
import datetime as dt
import json
import pandas as pd
import pytest

import config
from broker import Order
from tests.fakes import FakeBroker
from unittest.mock import MagicMock, patch
import os
import sys


def _portfolio_cache(tmp_path, monkeypatch, data):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def _seed_core_position(tmp_path, monkeypatch, **overrides):
    base = {
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3000.0, "unrealized_pl": 0.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }
    base.update(overrides)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 5000.0, "equity": 50_000.0,
        "positions": [base],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })


def _make_snap(positions, cash=5000.0, equity=50_000.0):
    from orders import PortfolioSnapshot
    return PortfolioSnapshot(
        synced_at="2026-05-10T14:00:00+00:00",
        alpaca_env="paper", cash=cash, equity=equity,
        positions=positions,
        tranches={"core": {"last_rebalance": "2026-05-10"},
                  "aggressive": {"last_rebalance": None}},
    )


def _stub_baseline(monkeypatch):
    from pending_plan import Baseline
    monkeypatch.setattr("baseline.capture_baseline",
                        lambda: Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                                         news_cursor_at=dt.datetime(2026, 5, 10, 14, 0, 0, tzinfo=dt.timezone.utc)))


def _stub_fetch_prices(monkeypatch, symbol: str, closes_values: list):
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=len(closes_values), freq="B")
    df = pd.DataFrame({symbol: closes_values}, index=idx)
    monkeypatch.setattr("data.fetch_prices",
                        lambda tickers, period="2y": df)


# ── 2R path ────────────────────────────────────────────────────

def test_check_sepa_exits_2r_path(tmp_path, monkeypatch):
    """At 2R, partial-sell 1/3, cancel trailing, re-trail at 2/3 qty."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch)
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)  # 2R hit
    fb.seed_open_order(Order(
        id="trail_old", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-old", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_old",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])

    notifications = check_sepa_exits(snap, fb)

    # Trailing cancelled
    assert "trail_old" in fb._canceled
    # New trailing submitted at 2/3 qty
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert len(new_trails) == 1
    assert new_trails[0].qty == pytest.approx(20.0, abs=0.01)
    # Partial sell queued to pending_plan
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and "sepa-2R" in s.intent.reason
               for s in plan.intents)
    # Telegram notification
    assert any("2R" in line for line in notifications)


# ── 3R path ────────────────────────────────────────────────────

def test_check_sepa_exits_3r_path(tmp_path, monkeypatch):
    """At 3R with 2R already filled, partial-sell 1/3, cancel trailing, NO re-trail."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch, shares=20.0,
                        market_value=2480.0, r_tier_filled=["2R"])
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 124.0)  # 3R hit
    fb.seed_open_order(Order(
        id="trail_2", symbol="AAPL", side="sell", type="trailing_stop",
        qty=20.0, notional=None, status="accepted",
        client_order_id="trail-2", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 20.0, "avg_entry": 100.0,
        "market_value": 2480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_2",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": ["2R"],
    }])

    notifications = check_sepa_exits(snap, fb)

    assert "trail_2" in fb._canceled
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert new_trails == []  # NO re-trail at 3R
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and "sepa-3R" in s.intent.reason
               for s in plan.intents)
    assert any("3R" in line for line in notifications)


# ── MA-break path ──────────────────────────────────────────────

def test_check_sepa_exits_ma_break_path(tmp_path, monkeypatch):
    """With r_tier_filled=['2R','3R'] and close < 21EMA, submit full exit."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch, shares=10.0,
                        market_value=1100.0, r_tier_filled=["2R", "3R"])
    _stub_baseline(monkeypatch)
    # Steady rise to 110 over 22 bars, then a drop to 80 (well below EMA).
    # MA-trail now reuses the close series from check_sepa_exits' batched
    # fetch_ohlcv (no separate fetch_prices call), so stub through ohlcv.
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL",
                              list(range(89, 111)) + [80.0])

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 110.0)

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 10.0, "avg_entry": 100.0,
        "market_value": 1100.0, "unrealized_pl": 100.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": ["2R", "3R"],
    }])

    notifications = check_sepa_exits(snap, fb)
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL"
               and "sepa-21EMA-break" in s.intent.reason
               for s in plan.intents)
    assert any("21EMA" in line for line in notifications)


# ── Guard paths ────────────────────────────────────────────────

def test_check_sepa_exits_skips_aggressive_tranche(tmp_path, monkeypatch):
    """Aggressive positions are bypassed entirely."""
    from watchdog import check_sepa_exits

    fb = FakeBroker()
    snap = _make_snap([{
        "symbol": "TQQQ", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 4000.0, "unrealized_pl": 1000.0,
        "tranche": "aggressive", "entry_reason": "agg rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 90.0, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []


def test_check_sepa_exits_skips_when_initial_stop_none(tmp_path, monkeypatch):
    from watchdog import check_sepa_exits

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 200.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 6000.0, "unrealized_pl": 3000.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": None, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []


def test_check_sepa_exits_disabled_when_config_off(tmp_path, monkeypatch):
    from watchdog import check_sepa_exits
    monkeypatch.setattr("config.SEPA_ENABLED", False)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []


# ── Phase 2 watchdog helpers ─────────────────────────────────────

def test_cancel_pending_partials_removes_sepa_sell_intents(tmp_path, monkeypatch):
    from watchdog import _cancel_pending_partials
    from pending_plan import (PENDING_PLAN_PATH as _, PendingPlan, IntentState, write_plan)
    from pending_plan import Baseline
    from orders import OrderIntent
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))

    write_plan(PendingPlan(
        plan_id="p-1", tranche="core",
        created_at=dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450, vix=14, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 5, 18, 14, 0, 0,
                                                     tzinfo=dt.timezone.utc)),
        intents=[
            IntentState(intent=OrderIntent(
                symbol="AAPL", notional=1000.0, side="sell",
                reason="sepa-2R", tranche="core", client_order_id="c1",
            )),
            IntentState(intent=OrderIntent(
                symbol="AAPL", notional=500.0, side="buy",
                reason="rebalance", tranche="core", client_order_id="c2",
            )),
            IntentState(intent=OrderIntent(
                symbol="NVDA", notional=800.0, side="sell",
                reason="sepa-3R", tranche="core", client_order_id="c3",
            )),
        ],
    ))

    _cancel_pending_partials("AAPL")

    from pending_plan import load_plan
    plan = load_plan()
    syms_reasons = [(s.intent.symbol, s.intent.reason) for s in plan.intents]
    # AAPL sepa-2R removed; AAPL buy preserved (different side); NVDA sepa-3R preserved.
    assert ("AAPL", "sepa-2R") not in syms_reasons
    assert ("AAPL", "rebalance") in syms_reasons
    assert ("NVDA", "sepa-3R") in syms_reasons


def test_cancel_pending_partials_noop_when_no_plan(tmp_path, monkeypatch):
    from watchdog import _cancel_pending_partials
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    _cancel_pending_partials("AAPL")  # must not raise


def test_set_climax_fired_updates_portfolio_cache(tmp_path, monkeypatch):
    from watchdog import _set_climax_fired
    import json

    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "2026-05-18T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "AAPL", "shares": 30, "avg_entry": 100.0,
             "market_value": 3000, "unrealized_pl": 0, "tranche": "core",
             "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": False},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-18"},
                     "aggressive": {"last_rebalance": None}},
    }))

    _set_climax_fired("AAPL")

    with open(tmp_path / "port.json") as f:
        cache = json.load(f)
    assert cache["positions"][0]["climax_fired"] is True


# ── Phase 2 failed-breakout integration ─────────────────────────

def _seed_entry_pivot(tmp_path, monkeypatch, symbol, pivot, entry_date):
    import json
    path = tmp_path / "pivots.json"
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text())
    existing[symbol] = {"pivot": pivot, "entry_date": entry_date}
    path.write_text(json.dumps(existing))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(path))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(path))


def _stub_fetch_ohlcv_closes(monkeypatch, symbol, closes_values, start="2026-05-15"):
    """Stub data.fetch_ohlcv to return a MultiIndex frame with the given closes."""
    import pandas as pd
    n = len(closes_values)
    idx = pd.date_range(start, periods=n, freq="B")
    df = pd.DataFrame({
        ("High",   symbol): [c + 0.5 for c in closes_values],
        ("Low",    symbol): [c - 0.5 for c in closes_values],
        ("Close",  symbol): closes_values,
        ("Volume", symbol): [1_000_000] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv",
                        lambda tickers, period="1y": df)


def test_check_sepa_exits_failed_breakout_full_exit_path(tmp_path, monkeypatch):
    """Day 2 close < pivot within window → cancel partial + submit_exit."""
    from watchdog import check_sepa_exits
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)  # AAPL entry@100, qty=30
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-15")
    # Closes: 100, 98 (Day 1 below pivot)
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 98.0],
                             start="2026-05-15")
    _stub_baseline(monkeypatch)

    # Pretend "today" is 2026-05-18 (so window covers Days 1-3).
    _real_datetime = dt.datetime
    class _FakeNowMod(_real_datetime):
        @classmethod
        def now(cls, tz=None): return _real_datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 98.0)

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2940.0, "unrealized_pl": -60.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)

    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    # The full exit landed in pending_plan with reason "sepa-failed-breakout".
    assert any(s.intent.symbol == "AAPL" and "failed-breakout" in s.intent.reason
               for s in plan.intents)
    assert any("failed-breakout" in line for line in notifications)


def test_check_sepa_exits_failed_breakout_cancels_pending_phase1_partial(tmp_path, monkeypatch):
    """Existing sepa-2R intent on AAPL is removed when failed-breakout fires."""
    from watchdog import check_sepa_exits
    from pending_plan import (PENDING_PLAN_PATH as _, PendingPlan, IntentState,
                              write_plan, load_plan, Baseline)
    from orders import OrderIntent
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-15")
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 98.0],
                             start="2026-05-15")
    _stub_baseline(monkeypatch)

    write_plan(PendingPlan(
        plan_id="p-1", tranche="core",
        created_at=dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450, vix=14, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 5, 18, 14, 0, 0,
                                                     tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="AAPL", notional=1000.0, side="sell",
            reason="sepa-2R", tranche="core", client_order_id="c1",
        ))],
    ))

    _real_datetime = dt.datetime
    class _FakeNowMod(_real_datetime):
        @classmethod
        def now(cls, tz=None): return _real_datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 98.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2940.0, "unrealized_pl": -60.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    plan = load_plan()
    # The original sepa-2R intent is gone; only failed-breakout intent remains.
    reasons = [s.intent.reason for s in plan.intents]
    assert "sepa-2R" not in reasons
    assert any("failed-breakout" in r for r in reasons)


def test_check_sepa_exits_failed_breakout_window_expired_skipped(tmp_path, monkeypatch):
    """Day 5 close below pivot → outside 3-day window → no failed-breakout."""
    from watchdog import check_sepa_exits
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-11")  # 5 trading days ago
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL",
                             [100.0, 101.0, 102.0, 101.5, 95.0],
                             start="2026-05-11")
    _stub_baseline(monkeypatch)

    _real_datetime = dt.datetime
    class _FakeNowMod(_real_datetime):
        @classmethod
        def now(cls, tz=None): return _real_datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 95.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2850.0, "unrealized_pl": -150.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)
    # No failed-breakout because window expired.
    assert not any("failed-breakout" in line for line in notifications)


def test_check_sepa_exits_gc_removes_exited_pivot_entries(tmp_path, monkeypatch):
    """A pivot for a symbol no longer in the portfolio is GC'd at end of pass."""
    from watchdog import check_sepa_exits
    import json

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Two pivot records — one for held AAPL, one for exited NVDA.
    (tmp_path / "pivots.json").write_text(json.dumps({
        "AAPL": {"pivot": 99.0, "entry_date": "2026-05-15"},
        "NVDA": {"pivot": 150.0, "entry_date": "2026-05-10"},  # exited
    }))

    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 101.0],
                             start="2026-05-15")

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 101.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3030.0, "unrealized_pl": 30.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    pivots = json.loads((tmp_path / "pivots.json").read_text())
    assert "AAPL" in pivots
    assert "NVDA" not in pivots


# ── Phase 2 climax integration ───────────────────────────────────

def _stub_fetch_ohlcv_full(monkeypatch, symbol, *,
                           close, high=None, low=None, volume=None,
                           start="2026-01-01"):
    import pandas as pd
    n = len(close)
    idx = pd.date_range(start, periods=n, freq="B")
    df = pd.DataFrame({
        ("High",   symbol): high or [c + 0.5 for c in close],
        ("Low",    symbol): low or [c - 0.5 for c in close],
        ("Close",  symbol): close,
        ("Volume", symbol): volume or [1_000_000] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv",
                        lambda tickers, period="1y": df)


def _climax_ohlcv(symbol):
    """Build OHLCV that satisfies all three climax conditions."""
    quiet_close = [100.0] * 50
    wild_close  = [102, 105, 108, 112, 116, 121, 126, 130.0]
    closes = quiet_close + wild_close
    quiet_high  = [100.5] * 50
    wild_high   = [c + 3 for c in wild_close]
    quiet_low   = [99.5] * 50
    wild_low    = [c - 3 for c in wild_close]
    quiet_vol   = [1_000_000] * 50
    wild_vol    = [4_000_000] * 8
    return dict(close=closes,
                high=quiet_high + wild_high,
                low=quiet_low + wild_low,
                volume=quiet_vol + wild_vol)


def test_check_sepa_exits_climax_sells_half_and_tightens_trail(tmp_path, monkeypatch):
    """All three climax conditions → sell 50% MV + submit tighter trailing."""
    from watchdog import check_sepa_exits
    from broker import Order

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    _seed_core_position(tmp_path, monkeypatch, shares=30.0,
                        market_value=3900.0,  # MV after run-up
                        r_tier_filled=[], climax_fired=False)
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", **_climax_ohlcv("AAPL"),
                           start="2026-01-01")
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)
    fb.seed_open_order(Order(
        id="trail_old", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="old-trail", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3900.0, "unrealized_pl": 900.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_old",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)

    # Old trailing cancelled, new one submitted with tighter %.
    assert "trail_old" in fb._canceled
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert len(new_trails) == 1
    # 50% partial sell submitted directly (not pending_plan).
    sell_orders = [o for o in fb._submitted if o.side == "sell" and o.type != "trailing_stop"]
    assert any(o.symbol == "AAPL" for o in sell_orders)
    assert any("climax" in line for line in notifications)


def test_check_sepa_exits_climax_sets_climax_fired_true(tmp_path, monkeypatch):
    """After climax, portfolio.json position has climax_fired=True."""
    from watchdog import check_sepa_exits
    import json

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    _seed_core_position(tmp_path, monkeypatch, shares=30.0, market_value=3900.0)
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", **_climax_ohlcv("AAPL"))
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3900.0, "unrealized_pl": 900.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    # _seed_core_position writes to "portfolio.json" via _portfolio_cache.
    with open(tmp_path / "portfolio.json") as f:
        cache = json.load(f)
    assert cache["positions"][0]["climax_fired"] is True


def test_check_sepa_exits_climax_disables_r_multiple_on_next_run(tmp_path, monkeypatch):
    """With climax_fired=True, R-multiple is gated off even at >2R price."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Use OHLCV that does NOT satisfy climax (return only, no range/vol).
    closes = [100.0] * 50 + [102, 105, 108, 112, 116, 121, 126, 130.0]
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", close=closes)
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)  # well past 2R target of 116
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
        "market_value": 1950.0, "unrealized_pl": 450.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
        "climax_fired": True,  # already fired
    }])
    notifications = check_sepa_exits(snap, fb)
    # No 2R/3R notification because climax_fired gates R-multiple.
    assert not any("2R hit" in line or "3R hit" in line for line in notifications)


def test_check_sepa_exits_climax_allows_ma_trail_after_fired(tmp_path, monkeypatch):
    """With climax_fired=True and close < 21EMA, full exit fires via MA-trail."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # OHLCV: rise then crash → climax_check False but ma_trail_should_exit True.
    closes = list(range(89, 111)) + [80.0]
    # submit_exit() reads cached position metadata to determine notional.
    _seed_core_position(tmp_path, monkeypatch, shares=15.0,
                        market_value=1200.0, climax_fired=True)
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", close=closes)
    # Phase 1's MA-trail reads via data.fetch_prices too — stub it equivalently.
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    monkeypatch.setattr("data.fetch_prices",
                        lambda tickers, period="2y":
                            pd.DataFrame({"AAPL": closes}, index=idx))
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
        "market_value": 1200.0, "unrealized_pl": -300.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
        "climax_fired": True,
    }])
    notifications = check_sepa_exits(snap, fb)
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL"
               and "sepa-21EMA-break" in s.intent.reason
               for s in plan.intents)
    assert any("21EMA" in line for line in notifications)


def test_check_price_moves_enforces_stop_with_market_sell(tmp_path, monkeypatch):
    import watchdog
    import pandas as pd
    from tests.fakes import FakeBroker

    monkeypatch.setattr("config.ENFORCE_STOPS", True)
    # No HALT file: point HALT_PATH at a non-existent tmp path.
    monkeypatch.setattr("config.HALT_PATH", str(tmp_path / "HALT"))

    # Price series ending well below entry → from_entry breaches core stop (-8%).
    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core",
    }], "cash": 0.0}

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)

    alerts = watchdog.check_price_moves(portfolio, broker=fb)

    sells = [o for o in fb._submitted if o.side == "sell"]
    assert len(sells) == 1
    assert sells[0].symbol == "AAPL"
    assert sells[0].qty == 3.5  # full fractional position sold (the whole point)
    assert any("STOP ENFORCED" in a[2] for a in alerts)


def test_check_price_moves_no_enforce_when_halted(tmp_path, monkeypatch):
    """HALT file present → breach is detected but no sell is submitted."""
    import watchdog
    import pandas as pd
    from tests.fakes import FakeBroker

    monkeypatch.setattr("config.ENFORCE_STOPS", True)
    halt = tmp_path / "HALT"
    halt.write_text("paused")  # HALT present
    monkeypatch.setattr("config.HALT_PATH", str(halt))

    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core",
    }], "cash": 0.0}

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)

    alerts = watchdog.check_price_moves(portfolio, broker=fb)

    assert [o for o in fb._submitted if o.side == "sell"] == []   # halted: no sell
    assert any("STOP-LOSS TRIGGERED" in a[2] for a in alerts)     # but still detected


def test_check_price_moves_alert_only_when_enforce_disabled(tmp_path, monkeypatch):
    import watchdog
    import pandas as pd
    from tests.fakes import FakeBroker

    monkeypatch.setattr("config.ENFORCE_STOPS", False)
    monkeypatch.setattr("config.HALT_PATH", str(tmp_path / "HALT"))

    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core",
    }], "cash": 0.0}

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)

    alerts = watchdog.check_price_moves(portfolio, broker=fb)

    assert [o for o in fb._submitted if o.side == "sell"] == []
    assert any("STOP-LOSS TRIGGERED" in a[2] for a in alerts)  # legacy alert intact


# ── I1: entry_date threaded into legacy positions for peak clamping ──

def test_as_legacy_positions_passes_entry_date_through():
    import watchdog, orders
    snap = orders.PortfolioSnapshot(
        synced_at="2026-06-24T00:00:00+00:00", alpaca_env="paper",
        cash=0.0, equity=1000.0,
        positions=[{"symbol": "AAPL", "shares": 5, "avg_entry": 100.0,
                    "market_value": 500.0, "unrealized_pl": 0.0,
                    "tranche": "core", "entry_reason": "adopted",
                    "entry_date": "2026-06-20"}],
        tranches={},
    )
    legacy = watchdog._as_legacy_positions(snap)
    assert legacy[0]["entry_date"] == "2026-06-20"


# ── I2: stop-enforce prunes the queued SEPA sell to avoid double-sell ──

def test_stop_enforce_cancels_pending_sepa_intent(tmp_path, monkeypatch):
    import watchdog, datetime as dt
    import pandas as pd
    from tests.fakes import FakeBroker
    from pending_plan import PendingPlan, IntentState, Baseline, write_plan, load_plan
    from orders import OrderIntent

    monkeypatch.setattr("config.ENFORCE_STOPS", True)
    monkeypatch.setattr("config.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))

    write_plan(PendingPlan(
        plan_id="p-1", tranche="core",
        created_at=dt.datetime(2026, 6, 24, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450, vix=14, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 6, 24, 14, 0, 0,
                                                     tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="AAPL", notional=1000.0, side="sell",
            reason="sepa-2R", tranche="core", client_order_id="c1"))],
    ))

    idx = pd.date_range(end=dt.date.today(), periods=5, freq="B")
    df = pd.DataFrame({"AAPL": [100.0, 100.0, 100.0, 100.0, 80.0]}, index=idx)
    monkeypatch.setattr("data.fetch_prices", lambda tickers, period="6mo": df)

    portfolio = {"positions": [{
        "ticker": "AAPL", "shares": 3.5, "entry_price": 100.0,
        "entry_date": "", "tranche": "core"}], "cash": 0.0}
    fb = FakeBroker(); fb.set_latest_price("AAPL", 80.0)

    watchdog.check_price_moves(portfolio, broker=fb)

    assert [o for o in fb._submitted if o.side == "sell"]   # a stop sell happened
    plan = load_plan()
    syms_reasons = [(s.intent.symbol, s.intent.reason) for s in plan.intents]
    assert ("AAPL", "sepa-2R") not in syms_reasons          # SEPA sell pruned
# ======================================================================
# Post-review additions (formerly test_watchdog_optimizations.py)
# ======================================================================

"""Regression tests for watchdog.py optimizations (W1, W6, W14, W18, W20,
W22, W42, W49, W50, W51).

These cover behaviors that the original watchdog test suite didn't pin down
because the optimizations changed semantics around dedup, fallback, batching,
and noise reduction."""
import datetime as dt
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _make_snap_opt(positions, cash=5000.0, equity=50_000.0):
    from orders import PortfolioSnapshot
    return PortfolioSnapshot(
        synced_at="2026-05-10T14:00:00+00:00",
        alpaca_env="paper", cash=cash, equity=equity,
        positions=positions, tranches={},
    )


# ── W18: check_rebalance is no longer always-noise ─────────────────

def test_rebalance_check_silent_when_recent(monkeypatch):
    """With daily cadence + today's bump, check_rebalance must emit zero alerts."""
    from watchdog import check_rebalance
    today = dt.date.today().isoformat()
    portfolio = {"positions": [], "last_rebalance": today}
    alerts = check_rebalance(portfolio)
    assert alerts == []


def test_rebalance_check_silent_within_grace_window(monkeypatch):
    """3 days ago is still inside the 7-day staleness window — no alert."""
    from watchdog import check_rebalance
    three_days = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    portfolio = {"positions": [], "last_rebalance": three_days}
    alerts = check_rebalance(portfolio)
    assert alerts == []


def test_rebalance_check_fires_when_truly_stale(monkeypatch):
    """> 7 days since rebalance → ONE critical alert about cron health."""
    from watchdog import check_rebalance
    stale = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    portfolio = {"positions": [], "last_rebalance": stale}
    alerts = check_rebalance(portfolio)
    assert len(alerts) == 1
    assert "CRITICAL" in alerts[0][0]
    assert "10 days" in alerts[0][2]


# ── W14: buy-signal tranche is "core" not "stock" ──────────────────

def test_check_buy_signals_uses_core_tranche(tmp_path, monkeypatch):
    """Submitted intent must carry tranche='core' so sync_state preserves it."""
    import watchdog, orders, config
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "po.json"))
    monkeypatch.setattr(watchdog, "_BUY_SIGNALS_TODAY_PATH",
                        str(tmp_path / "buy_sig.json"))

    # Force the screener cache to a single-candidate result
    monkeypatch.setattr(watchdog, "_get_screened_stocks",
                        lambda: pd.DataFrame([{"ticker": "NVDA"}]))
    # Build the buy-signal preconditions:
    #   ≥1 down day in the lookback window (iloc[:-1] strips today),
    #   est_vol > max_down_vol.
    n = 15
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    # Mostly up, with a clear down day in the middle (bar 7: 106 → 102)
    closes = [100, 101, 102, 103, 104, 105, 106, 102, 103, 104,
              105, 106, 107, 108, 109]
    vols = [1_000_000] * n
    vols[7] = 2_000_000   # the down day has volume 2M
    hist_frame = pd.DataFrame(
        {("NVDA", "Close"): closes, ("NVDA", "Volume"): vols}, index=idx,
    )
    hist_frame.columns = pd.MultiIndex.from_tuples(hist_frame.columns)
    intraday_frame = pd.DataFrame(
        {("NVDA", "Volume"): [200_000] * 10},
        index=pd.date_range("2026-05-15 09:30", periods=10, freq="1min"),
    )
    intraday_frame.columns = pd.MultiIndex.from_tuples(intraday_frame.columns)

    def fake_download(tickers, **_kw):
        if _kw.get("interval") == "1m":
            return intraday_frame
        return hist_frame
    monkeypatch.setattr("yfinance.download", fake_download)
    monkeypatch.setattr(watchdog, "_estimate_full_day_volume",
                        lambda t, bars=None: 50_000_000)  # forces signal

    from tests.fakes import FakeBroker
    fb = FakeBroker(cash=100_000, equity=100_000)
    fb.set_latest_price("NVDA", 200.0)

    snap = _make_snap_opt([])
    lines = watchdog.check_buy_signals(snap, fb)

    # Should have submitted one buy with tranche="core"
    assert any("BUY SIGNAL" in l for l in lines)
    submitted = [o for o in fb._submitted]
    assert len(submitted) == 1


# ── W20: snapshot falls back to cache on broker failure ────────────

def test_snapshot_falls_back_to_cache_on_broker_failure(tmp_path, monkeypatch):
    """If sync_state raises twice, snapshot returns a cache-backed snapshot
    instead of crashing — so SEPA / stop-loss checks can still run."""
    import watchdog, orders, config
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "tg.json"))
    monkeypatch.setattr(watchdog, "_DEGRADED_SENTINEL_PATH",
                        str(tmp_path / "degraded.json"))

    # Pre-seed a portfolio.json so the fallback has something to read
    cache = {
        "synced_at": "2026-05-10T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 1000.0, "equity": 50_000.0,
        "positions": [{"symbol": "AAPL", "shares": 10, "avg_entry": 100,
                       "market_value": 1100, "unrealized_pl": 100,
                       "tranche": "core", "entry_reason": "x"}],
        "tranches": {"core": {"last_rebalance": "2026-05-10"}},
    }
    (tmp_path / "p.json").write_text(json.dumps(cache))

    def always_fail(*a, **kw):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(orders, "sync_state", always_fail)

    broker = MagicMock()
    snap = watchdog.snapshot(broker=broker)
    # Cache-backed snap has the AAPL position
    assert any(p["symbol"] == "AAPL" for p in snap.positions)
    assert snap.cash == 1000.0
    # TG notification was queued
    notifs = json.loads((tmp_path / "tg.json").read_text())
    assert any("CRITICAL" in n["message"] for n in notifs)


# ── W22: macro flip — act first, persist score only on success ─────

def test_check_macro_shift_skips_score_persist_when_act_fails(tmp_path, monkeypatch):
    """If act_on_macro_flip raises, the new score must NOT be written.
    Otherwise tomorrow's run sees prev_regime=new and never retries the exit."""
    import watchdog
    score_file = tmp_path / "last_macro_score.json"
    monkeypatch.setattr(watchdog, "_MACRO_SCORE_PATH", str(score_file))

    # Pre-seed prev score
    score_file.write_text(json.dumps({"score": 0.5, "regime": "expansion"}))

    monkeypatch.setattr("macro.macro_regime_score", lambda: {
        "score": -0.5, "regime": "contraction",
        "indicators": {},
    })

    def boom(*a, **kw):
        raise RuntimeError("alpaca down mid-act")
    monkeypatch.setattr(watchdog, "act_on_macro_flip", boom)

    snap = _make_snap_opt([])
    alerts, _ = watchdog.check_macro_shift(snap)

    # Score file should still contain the OLD score (act failed → no persist)
    stored = json.loads(score_file.read_text())
    assert stored["score"] == 0.5
    assert stored["regime"] == "expansion"
    # Alert about the failure should appear
    assert any("alpaca down mid-act" in a[2] for a in alerts)


# ── W6: peak window uses entry_date when available ─────────────────

def test_check_price_moves_peak_excludes_pre_entry_history(tmp_path, monkeypatch):
    """A 6-month high BEFORE entry must not appear as the trailing-stop peak.

    Setup: AAPL spiked to $200 last month, then dropped to $100 entry, now at $150.
    The "from peak" warning should reference the post-entry peak ($150), not $200."""
    import watchdog
    import pandas as pd

    entry_date = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    portfolio = {
        "positions": [{
            "ticker": "AAPL", "shares": 10, "entry_price": 100.0,
            "entry_date": entry_date, "tranche": "core",
        }],
        "cash": 0.0,
    }
    # 6 months of prices: ramp 50 → 200 (pre-entry spike), drop to 100, climb to 150
    n = 126
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    pre = list(range(50, 200))[:n - 20] if n > 20 else []
    # Make sure series ends near today's date by using a fresh range
    idx = pd.date_range(end=dt.date.today(), periods=n, freq="B")
    # First 100 bars climb to 200, then drop to 100, then climb to 150
    values = (list(range(101, 201))                 # bars 0..99, ending 200
              + [100.0] * 10                         # entry-ish prices
              + list(range(101, 117)))               # climb to 116
    if len(values) > n:
        values = values[:n]
    while len(values) < n:
        values.append(values[-1])
    df = pd.DataFrame({"AAPL": values}, index=idx)
    monkeypatch.setattr("data.fetch_prices",
                        lambda tickers, period="6mo": df)

    alerts = watchdog.check_price_moves(portfolio)
    # No CRITICAL trailing-stop alert should fire from a pre-entry high.
    crit_trail = [a for a in alerts if "TRAILING STOP HIT" in a[2]]
    assert crit_trail == []


# ── W49: U-shape volume projection ────────────────────────────────

def test_intraday_volume_fraction_open_burst():
    from watchdog import _intraday_volume_fraction
    # 30 min in → halfway through the opening 25% block → ~12.5%
    assert abs(_intraday_volume_fraction(30) - 0.125) < 0.001


def test_intraday_volume_fraction_midday_trough():
    from watchdog import _intraday_volume_fraction
    # 195 min in (~12:45) → 25% + (135/270) × 50% = 25% + 25% = 50%
    assert abs(_intraday_volume_fraction(195) - 0.50) < 0.001


def test_intraday_volume_fraction_closing_burst():
    from watchdog import _intraday_volume_fraction
    # 360 min in (~15:30) → 75% + (30/60) × 25% = 87.5%
    assert abs(_intraday_volume_fraction(360) - 0.875) < 0.001


def test_intraday_volume_fraction_after_close():
    from watchdog import _intraday_volume_fraction
    # 400 min — past 16:00, caps at 1.0
    assert _intraday_volume_fraction(400) == 1.0


def test_intraday_volume_fraction_zero_at_open():
    from watchdog import _intraday_volume_fraction
    assert _intraday_volume_fraction(0) == 0.0
    assert _intraday_volume_fraction(-5) == 0.0


# ── W50: buy signals dedup across ticks ───────────────────────────

def test_buy_signals_today_dedup(tmp_path, monkeypatch):
    """Once a ticker fires today, _load_today_buy_signals returns it."""
    import watchdog
    monkeypatch.setattr(watchdog, "_BUY_SIGNALS_TODAY_PATH",
                        str(tmp_path / "bs.json"))
    assert watchdog._load_today_buy_signals() == set()
    watchdog._record_today_buy_signal("NVDA")
    watchdog._record_today_buy_signal("AAPL")
    assert watchdog._load_today_buy_signals() == {"NVDA", "AAPL"}


def test_buy_signals_resets_on_date_change(tmp_path, monkeypatch):
    """Yesterday's entries don't count for today's dedup."""
    import watchdog
    monkeypatch.setattr(watchdog, "_BUY_SIGNALS_TODAY_PATH",
                        str(tmp_path / "bs.json"))
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    (tmp_path / "bs.json").write_text(json.dumps({
        "date": yesterday, "tickers": ["STALE"]
    }))
    assert watchdog._load_today_buy_signals() == set()


# ── W48: timeutils shared helpers ─────────────────────────────────

def test_timeutils_is_rth_now_weekend():
    """Weekend should always return False regardless of time."""
    import timeutils
    saturday_noon = dt.datetime(2026, 4, 18, 12, 0)   # Sat
    sunday_noon = dt.datetime(2026, 4, 19, 12, 0)     # Sun
    with patch.object(timeutils, "now_et", return_value=saturday_noon):
        assert timeutils.is_rth_now() is False
    with patch.object(timeutils, "now_et", return_value=sunday_noon):
        assert timeutils.is_rth_now() is False


def test_timeutils_is_rth_now_weekday_in_session():
    import timeutils
    weekday_noon = dt.datetime(2026, 4, 17, 12, 0)   # Fri
    with patch.object(timeutils, "now_et", return_value=weekday_noon):
        assert timeutils.is_rth_now() is True


def test_timeutils_is_rth_now_weekday_after_close():
    import timeutils
    weekday_evening = dt.datetime(2026, 4, 17, 18, 0)
    with patch.object(timeutils, "now_et", return_value=weekday_evening):
        assert timeutils.is_rth_now() is False


# ── W17: _set_climax_fired writes atomically ──────────────────────

def test_set_climax_fired_locks_and_writes(tmp_path, monkeypatch):
    import watchdog, orders
    portfolio_path = tmp_path / "p.json"
    cache = {
        "positions": [
            {"symbol": "AAPL", "climax_fired": False},
            {"symbol": "NVDA", "climax_fired": False},
        ],
    }
    portfolio_path.write_text(json.dumps(cache))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(portfolio_path))

    watchdog._set_climax_fired("AAPL")
    after = json.loads(portfolio_path.read_text())
    by_sym = {p["symbol"]: p for p in after["positions"]}
    assert by_sym["AAPL"]["climax_fired"] is True
    assert by_sym["NVDA"]["climax_fired"] is False


def test_set_climax_fired_noop_on_missing_file(tmp_path, monkeypatch):
    """No portfolio.json → no error, just return silently."""
    import watchdog, orders
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "no_such.json"))
    watchdog._set_climax_fired("AAPL")   # must not raise


# ── P14: buy signal dedup ONLY stamps on successful submit ────────

def test_buy_signal_not_stamped_when_submit_skipped(tmp_path, monkeypatch):
    """If execute_plan returns skipped (HALT, cash gate), don't stamp dedup —
    a later tick on the same day should still get a chance to fire."""
    import watchdog, orders, config
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "po.json"))
    monkeypatch.setattr(watchdog, "_BUY_SIGNALS_TODAY_PATH",
                        str(tmp_path / "bs.json"))

    # Force HALT so execute_plan skips every intent
    (tmp_path / "HALT").touch()

    monkeypatch.setattr(watchdog, "_get_screened_stocks",
                        lambda: pd.DataFrame([{"ticker": "NVDA"}]))
    n = 15
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    closes = [100, 101, 102, 103, 104, 105, 106, 102, 103, 104,
              105, 106, 107, 108, 109]
    vols = [1_000_000] * n
    vols[7] = 2_000_000
    hist_frame = pd.DataFrame(
        {("NVDA", "Close"): closes, ("NVDA", "Volume"): vols}, index=idx,
    )
    hist_frame.columns = pd.MultiIndex.from_tuples(hist_frame.columns)
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: hist_frame)
    monkeypatch.setattr(watchdog, "_estimate_full_day_volume",
                        lambda t, bars=None: 50_000_000)

    from tests.fakes import FakeBroker
    fb = FakeBroker(cash=100_000, equity=100_000)
    fb.set_latest_price("NVDA", 200.0)

    lines = watchdog.check_buy_signals(_make_snap_opt([]), fb)
    # Signal fired but submit was HALT'd
    assert any("BUY SIGNAL" in l for l in lines)
    # Dedup stamp must NOT have been written
    assert watchdog._load_today_buy_signals() == set()


# ── P3: degraded mode notifies only on transitions ───────────────

def test_snapshot_degraded_notifies_only_on_transition(tmp_path, monkeypatch):
    """5 consecutive degraded snapshots → ONE TG notification, not 5."""
    import watchdog, orders, config
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "tg.json"))
    monkeypatch.setattr(watchdog, "_DEGRADED_SENTINEL_PATH",
                        str(tmp_path / "degraded.json"))

    # Seed minimal cache so fallback can build a snap
    (tmp_path / "p.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper",
        "cash": 1000.0, "equity": 1000.0,
        "positions": [], "tranches": {},
    }))

    def always_fail(*a, **kw):
        raise RuntimeError("alpaca down")
    monkeypatch.setattr(orders, "sync_state", always_fail)

    broker = MagicMock()
    # Fire 5 ticks — all degraded
    for _ in range(5):
        watchdog.snapshot(broker=broker)

    notifs = json.loads((tmp_path / "tg.json").read_text())
    degraded_msgs = [n for n in notifs if "DEGRADED" in n.get("message", "")]
    # Exactly one — sentinel suppressed the other 4
    assert len(degraded_msgs) == 1


def test_snapshot_recovery_notifies_once(tmp_path, monkeypatch):
    """degraded → healthy transition emits one RECOVERED notification."""
    import watchdog, orders, config
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(config, "TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "tg.json"))
    monkeypatch.setattr(watchdog, "_DEGRADED_SENTINEL_PATH",
                        str(tmp_path / "degraded.json"))

    (tmp_path / "p.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [], "tranches": {},
    }))

    # Call 1: both retry attempts fail → degraded mode + sentinel + notif
    fail_mode = {"on": True}
    def maybe_fail(*a, **kw):
        if fail_mode["on"]:
            raise RuntimeError("blip")
        return orders.PortfolioSnapshot(
            synced_at="x", alpaca_env="paper", cash=0.0, equity=0.0,
            positions=[], tranches={},
        )
    monkeypatch.setattr(orders, "sync_state", maybe_fail)

    broker = MagicMock()
    watchdog.snapshot(broker=broker)  # both retries fail → degraded
    fail_mode["on"] = False           # broker recovers
    watchdog.snapshot(broker=broker)  # succeeds → emit RECOVERED

    notifs = json.loads((tmp_path / "tg.json").read_text())
    recovered = [n for n in notifs if "RECOVERED" in n.get("message", "")]
    assert len(recovered) == 1
    # Sentinel cleared
    assert not os.path.exists(tmp_path / "degraded.json")


# ── P5: SEPA live_prices uses batched latest_quote ────────────────

def test_check_sepa_exits_live_prices_calls_latest_quote(tmp_path, monkeypatch):
    """When live_prices=True, current_price comes from broker.latest_quote
    (mid), NOT from snap.market_value/shares."""
    import watchdog, orders, config, sepa_exits
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(config, "SEPA_ENABLED", True)

    # Stub OHLCV (irrelevant data — just needs to be present)
    idx = pd.date_range("2026-05-01", periods=30, freq="B")
    df = pd.DataFrame({
        ("Close",  "AAPL"): [100.0] * 30,
        ("High",   "AAPL"): [101.0] * 30,
        ("Low",    "AAPL"): [99.0]  * 30,
        ("Volume", "AAPL"): [1_000_000] * 30,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv", lambda *a, **kw: df)

    # Make next_r_tier_action fire so we observe current_price was used
    captured = {}
    real_action = sepa_exits.next_r_tier_action
    def spy(pos, current_price):
        captured["price"] = current_price
        return None  # don't actually trigger a sell
    monkeypatch.setattr(sepa_exits, "next_r_tier_action", spy)

    snap = _make_snap_opt([{
        "symbol": "AAPL", "shares": 10.0, "avg_entry": 100.0,
        # market_value/shares would be 90 — a stale price
        "market_value": 900.0, "unrealized_pl": -100.0,
        "tranche": "core", "entry_reason": "x",
        "initial_entry_price": 100.0, "initial_qty": 10,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])

    from tests.fakes import FakeBroker
    fb = FakeBroker()
    # Live quote says 150 (real-time) — different from snap-derived 90
    fb.set_latest_quote("AAPL", bid=149.0, ask=151.0)

    watchdog.check_sepa_exits(snap, fb, live_prices=True)
    # Should have used live quote mid (150), not snap stale (90)
    assert captured["price"] == pytest.approx(150.0, abs=0.5)


def test_check_sepa_exits_default_uses_snap_price(tmp_path, monkeypatch):
    """Without live_prices, current_price falls back to snap-derived."""
    import watchdog, orders, config, sepa_exits
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(config, "SEPA_ENABLED", True)

    idx = pd.date_range("2026-05-01", periods=30, freq="B")
    df = pd.DataFrame({
        ("Close",  "AAPL"): [100.0] * 30,
        ("High",   "AAPL"): [101.0] * 30,
        ("Low",    "AAPL"): [99.0]  * 30,
        ("Volume", "AAPL"): [1_000_000] * 30,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv", lambda *a, **kw: df)

    captured = {}
    def spy(pos, current_price):
        captured["price"] = current_price
        return None
    monkeypatch.setattr(sepa_exits, "next_r_tier_action", spy)

    snap = _make_snap_opt([{
        "symbol": "AAPL", "shares": 10.0, "avg_entry": 100.0,
        "market_value": 900.0,   # snap-derived = 90
        "unrealized_pl": -100.0,
        "tranche": "core", "entry_reason": "x",
        "initial_entry_price": 100.0, "initial_qty": 10,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])

    from tests.fakes import FakeBroker
    fb = FakeBroker()
    fb.set_latest_quote("AAPL", bid=149.0, ask=151.0)  # live quote present
    # But default live_prices=False → ignore live quote, use snap-derived

    watchdog.check_sepa_exits(snap, fb)   # no live_prices kw
    assert captured["price"] == pytest.approx(90.0, abs=0.01)


# ── P7: log_daily append-only ─────────────────────────────────────

def test_log_daily_appends_one_row_only(tmp_path, monkeypatch):
    """Daily call writes exactly one CSV row; second same-day call no-ops."""
    import watchdog
    log_path = tmp_path / "daily_log.csv"
    monkeypatch.setattr("watchdog.os.path.dirname",
                        lambda _p: str(tmp_path))

    portfolio = {"positions": [{"ticker": "X"}], "cash": 100.0}
    watchdog.log_daily(portfolio, total_value=1100.0, total_pnl_pct=10.0)
    watchdog.log_daily(portfolio, total_value=1200.0, total_pnl_pct=20.0)

    content = log_path.read_text().strip().splitlines()
    assert len(content) == 2  # header + one row
    assert content[0].startswith("date,")
    assert content[1].count(",") == 5


