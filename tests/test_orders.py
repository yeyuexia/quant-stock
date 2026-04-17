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
