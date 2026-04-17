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
