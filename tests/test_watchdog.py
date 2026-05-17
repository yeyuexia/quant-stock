"""Integration tests for watchdog SEPA orchestration against FakeBroker."""
import datetime as dt
import json
import pandas as pd
import pytest

import config
from broker import Order
from tests.fakes import FakeBroker


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
    _stub_fetch_prices(monkeypatch, "AAPL",
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
