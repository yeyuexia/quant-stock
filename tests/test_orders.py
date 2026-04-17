"""Unit tests for orders.py — heaviest coverage in the codebase (safety rails)."""
import datetime as dt
import json
import os
import re
import pytest

from broker import BrokerError
from tests.fakes import FakeBroker, FakeClock


def test_make_cid_format():
    from orders import _make_cid
    cid = _make_cid(tranche="core", reason="rebalance", symbol="SPY",
                    today=dt.date(2026, 4, 17))
    assert re.fullmatch(r"core-rebalance-SPY-20260417-[0-9a-f]{6}", cid), cid


def test_make_cid_deterministic_per_day():
    from orders import _make_cid
    a = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    b = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    assert a == b


def test_make_cid_varies_by_day():
    from orders import _make_cid
    a = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    b = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 18))
    assert a != b


# ── sync_state ──────────────────────────────────────────────────

def _portfolio_cache(tmp_path, monkeypatch, data):
    """Point PORTFOLIO_PATH/etc at tmp dir and seed portfolio.json."""
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def test_sync_state_carries_forward_known_tranche(tmp_path, monkeypatch):
    from orders import sync_state

    old = {
        "synced_at": "2026-04-16T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance 2026-04-16",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-04-16"},
                     "aggressive": {"last_rebalance": "2026-04-16"}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500, mv=5050)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["tranche"] == "core"
    assert p["entry_reason"] == "core rebalance 2026-04-16"
    assert p["market_value"] == 5050


def test_sync_state_marks_unknown_tranche(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, None)  # no cache

    fb = FakeBroker()
    fb.seed_position("NVDA", qty=5, avg_entry=100, mv=520)

    alerts: list = []
    snap = sync_state(fb, alerts=alerts)

    assert snap.positions[0]["tranche"] == "unknown"
    assert any("unknown" in a.lower() and "NVDA" in a for a in alerts)


def test_sync_state_drops_closed_positions(tmp_path, monkeypatch):
    from orders import sync_state

    old = {
        "synced_at": "2026-04-16T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-04-16"},
                     "aggressive": {"last_rebalance": "2026-04-16"}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    fb = FakeBroker()  # no positions seeded — SPY was closed

    snap = sync_state(fb, alerts=[])
    assert snap.positions == []


def test_sync_state_flags_missing_bracket(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, None)
    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500)
    # no open orders seeded => bracket is missing

    alerts: list = []
    sync_state(fb, alerts=alerts)
    assert any("bracket" in a.lower() and "SPY" in a for a in alerts)


# ── reconcile_to_targets ────────────────────────────────────────

def _snap(positions, cash=10_000, equity=100_000):
    from orders import PortfolioSnapshot
    return PortfolioSnapshot(
        synced_at="2026-04-17T14:00:00+00:00",
        alpaca_env="paper",
        cash=cash, equity=equity,
        positions=positions,
        tranches={"core": {"last_rebalance": None},
                  "aggressive": {"last_rebalance": None}},
    )


def test_reconcile_opens_new_positions(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    snap = _snap(positions=[], cash=90_000, equity=90_000)
    plan = reconcile_to_targets(
        {"SPY": 0.5, "QQQ": 0.5},
        tranche="core",
        snapshot=snap,
        tranche_capital=90_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 2
    buy_syms = sorted(i.symbol for i in plan.buys)
    assert buy_syms == ["QQQ", "SPY"]
    assert all(i.notional == 45_000 for i in plan.buys)
    assert all(i.tranche == "core" for i in plan.buys)
    assert all(i.stop_pct is not None and i.trail_pct is not None for i in plan.buys)


def test_reconcile_closes_removed_positions(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "TSLA", "shares": 10, "avg_entry": 300,
         "market_value": 3000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    sell_syms = [i.symbol for i in plan.sells]
    assert sell_syms == ["TSLA"]
    assert plan.sells[0].notional == 3000


def test_reconcile_ignores_unknown_tranche(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "NVDA", "shares": 5, "avg_entry": 100,
         "market_value": 520, "unrealized_pl": 0,
         "tranche": "unknown", "entry_reason": "external",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    # NVDA is unknown — should not be sold
    assert all(i.symbol != "NVDA" for i in plan.sells)


def test_reconcile_rebalance_within_tranche(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "SPY", "shares": 10, "avg_entry": 500,
         "market_value": 6000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
        {"symbol": "QQQ", "shares": 5, "avg_entry": 400,
         "market_value": 2000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 0.4, "QQQ": 0.4, "IWM": 0.2},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    # Targets: SPY $4000 (down from $6000), QQQ $4000 (up from $2000), IWM $2000 new
    got = {i.symbol: (i.side, i.notional) for i in plan.buys + plan.sells}
    assert got["SPY"] == ("sell", 2000)
    assert got["QQQ"] == ("buy", 2000)
    assert got["IWM"] == ("buy", 2000)


# ── HALT ────────────────────────────────────────────────────────

def _safety_paths(tmp_path, monkeypatch):
    """Redirect all safety-rail paths into tmp_path."""
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))


def test_halt_blocks_all_orders(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    (tmp_path / "HALT").write_text("paused")

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=500, side="buy",
                           reason="test", tranche="core",
                           client_order_id="cid-1",
                           stop_pct=0.08, trail_pct=0.12)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert result.submitted == []
    assert len(result.skipped) == 1
    assert "HALT" in result.skipped[0][1]


# ── Daily caps ──────────────────────────────────────────────────

def _intent(sym, notional, side="buy"):
    from orders import OrderIntent
    return OrderIntent(
        symbol=sym, notional=notional, side=side,
        reason="test", tranche="core",
        client_order_id=f"core-test-{sym}-20260417-abcdef",
        stop_pct=0.08 if side == "buy" else None,
        trail_pct=0.12 if side == "buy" else None,
    )


def test_daily_max_orders_cap(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 2)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("A", 100), _intent("B", 100), _intent("C", 100)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert len(result.submitted) == 2
    assert len(result.deferred) == 1


def test_daily_max_notional_cap(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 500)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("A", 300), _intent("B", 300)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert len(result.submitted) == 1
    assert len(result.deferred) == 1


def test_caps_persist_across_calls(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 2)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("A", 100)], sells=[], holds=[]),
                 broker=fb, reason="t1")
    execute_plan(OrderPlan(buys=[_intent("B", 100)], sells=[], holds=[]),
                 broker=fb, reason="t2")
    # Third call should defer — already at 2 submitted today
    r3 = execute_plan(OrderPlan(buys=[_intent("C", 100)], sells=[], holds=[]),
                       broker=fb, reason="t3")
    assert r3.submitted == []
    assert len(r3.deferred) == 1


# ── Large-order gate ────────────────────────────────────────────

def test_large_order_queued_not_submitted(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("SMALL", 500), _intent("BIG", 2_500)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert [o.symbol for o in result.submitted] == ["SMALL"]
    assert [i.symbol for i in result.queued] == ["BIG"]

    pending = json.loads((tmp_path / "pending_orders.json").read_text())
    assert len(pending) == 1 and pending[0]["symbol"] == "BIG"
    assert "expires" in pending[0]


def test_approve_pending_submits(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    assert len(pending) == 1
    result = approve_pending(pending[0]["id"], broker=fb)
    assert len(result.submitted) == 1
    assert list_pending() == []


def test_reject_pending_removes(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, reject_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    reject_pending(pending[0]["id"])
    assert list_pending() == []


def test_approve_expired_rejected(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)
    monkeypatch.setattr("orders.PENDING_ORDER_TTL_HOURS", 0)  # expires instantly

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    result = approve_pending(pending[0]["id"], broker=fb)
    assert result.submitted == []
    assert any("expired" in msg.lower() for _, msg in result.skipped)
    assert list_pending() == []


# ── submit_exit ─────────────────────────────────────────────────

def test_submit_exit_sells_full_position(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    old = {
        "synced_at": "2026-04-16T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "TQQQ", "shares": 50.0, "avg_entry": 60.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "aggressive", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import submit_exit
    fb = FakeBroker()
    fb.seed_position("TQQQ", qty=50, avg_entry=60, mv=3000)

    result = submit_exit("TQQQ", reason="macro→contraction", broker=fb)
    assert len(result.submitted) == 1
    o = result.submitted[0]
    assert o.symbol == "TQQQ" and o.side == "sell"


def test_submit_exit_respects_halt(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "TQQQ", "shares": 50.0, "avg_entry": 60.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "aggressive", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    })
    (tmp_path / "HALT").touch()

    from orders import submit_exit
    fb = FakeBroker()
    result = submit_exit("TQQQ", reason="macro→contraction", broker=fb)
    assert result.submitted == []
    assert any("HALT" in msg for _, msg in result.skipped)
