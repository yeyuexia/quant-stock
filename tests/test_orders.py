"""Unit tests for orders.py — heaviest coverage in the codebase (safety rails)."""
import datetime as dt
import json
import os
import re
import pytest

from broker import BrokerError
from tests.fakes import FakeBroker, FakeClock
from orders import OrderIntent


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

def test_submit_exit_queues_high_tier_intent(tmp_path, monkeypatch):
    """submit_exit now writes a HIGH-tier intent to pending_plan (not direct submit).

    The intent is preserved: exit is unconditionally queued for the executor to slice
    out. No order reaches the broker here — slicing happens on the next executor tick.
    """
    import baseline as bl
    _safety_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 18.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: -0.25)

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
    from pending_plan import load_plan
    fb = FakeBroker()
    fb.seed_position("TQQQ", qty=50, avg_entry=60, mv=3000)
    fb.set_latest_price("TQQQ", 60.0)

    result = submit_exit("TQQQ", reason="macro→contraction", broker=fb)

    # New behaviour: queued to pending_plan, not directly submitted.
    assert len(result.queued) == 1
    assert result.queued[0].symbol == "TQQQ"
    assert result.queued[0].side == "sell"
    assert result.queued[0].tier == "HIGH"
    assert result.submitted == []

    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "TQQQ" for s in plan.intents)


def test_submit_exit_conflict_falls_back_to_direct(tmp_path, monkeypatch):
    """When pending_plan already has an intent for the symbol, submit_exit falls
    back to direct execute_plan to avoid double-selling. HALT blocks that path.
    """
    import baseline as bl
    _safety_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 18.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: -0.25)

    # Pre-populate pending plan with a conflicting TQQQ intent.
    import datetime as _dt
    from pending_plan import write_plan, PendingPlan, IntentState, Baseline
    from orders import OrderIntent
    existing_intent = OrderIntent(
        symbol="TQQQ", notional=3000.0, side="sell",
        reason="rebalance", tranche="aggressive",
        client_order_id="aggressive-rebalance-TQQQ-existing",
    )
    conflict_plan = PendingPlan(
        plan_id="rebalance-conflict",
        tranche="aggressive",
        created_at=_dt.datetime(2026, 4, 17, 14, 0, 0, tzinfo=_dt.timezone.utc),
        baseline=Baseline(spy=480.0, vix=18.0, macro_score=-0.25,
                          news_cursor_at=_dt.datetime(2026, 4, 17, tzinfo=_dt.timezone.utc)),
        intents=[IntentState(intent=existing_intent)],
    )
    write_plan(conflict_plan)

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

    # Conflict path hits execute_plan → HALT blocks it → skipped, not submitted.
    assert result.submitted == []
    assert any("HALT" in msg for _, msg in result.skipped)


# ── tag_position ────────────────────────────────────────────────

def test_tag_position_updates_metadata(tmp_path, monkeypatch):
    old = {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "NVDA", "shares": 5, "avg_entry": 100,
             "market_value": 520, "unrealized_pl": 0,
             "tranche": "unknown", "entry_reason": "external",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    from orders import tag_position
    tag_position("NVDA", tranche="core", entry_reason="manual 2026-04-17")

    got = json.loads((tmp_path / "portfolio.json").read_text())
    pos = got["positions"][0]
    assert pos["tranche"] == "core"
    assert pos["entry_reason"] == "manual 2026-04-17"


def test_tag_position_bad_tranche_raises(tmp_path, monkeypatch):
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [], "tranches": {"core": {"last_rebalance": None},
                                       "aggressive": {"last_rebalance": None}},
    })
    from orders import tag_position
    with pytest.raises(ValueError):
        tag_position("NVDA", tranche="invalid")


# ── Critical fixes: market-open gate ────────────────────────────

def test_execute_plan_skips_when_market_closed(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)

    from orders import OrderPlan, execute_plan
    fb = FakeBroker()
    fb.market_open = False
    plan = OrderPlan(buys=[_intent("A", 500)], sells=[], holds=[])
    result = execute_plan(plan, broker=fb, reason="test")
    assert result.submitted == []
    assert len(result.skipped) == 1
    assert "market closed" in result.skipped[0][1].lower()


# ── Critical fixes: approve_pending non-destructive ─────────────

def test_approve_pending_halt_preserves_queue(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    assert len(list_pending()) == 1

    # HALT active at approval time
    (tmp_path / "HALT").write_text("paused")
    pid = list_pending()[0]["id"]
    result = approve_pending(pid, broker=fb)

    assert result.submitted == []
    assert any("HALT" in msg for _, msg in result.skipped)
    # Order remains in queue
    assert len(list_pending()) == 1
    assert list_pending()[0]["id"] == pid


def test_approve_pending_cap_preserves_queue(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)
    # Normal caps so the order queues first
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pid = list_pending()[0]["id"]

    # Squeeze the cap before approval
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 0)
    result = approve_pending(pid, broker=fb)
    assert result.submitted == []
    assert any("cap" in msg.lower() for _, msg in result.skipped)
    # Order stays pending so the user can retry tomorrow
    assert len(list_pending()) == 1


def test_approve_pending_market_closed_preserves_queue(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")

    fb.market_open = False
    pid = list_pending()[0]["id"]
    result = approve_pending(pid, broker=fb)
    assert result.submitted == []
    assert any("market closed" in msg.lower() for _, msg in result.skipped)
    assert len(list_pending()) == 1


# ── Critical fixes: ensure_trailing_stops ───────────────────────

def test_ensure_trailing_stops_attaches_to_new_positions(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    })

    from orders import ensure_trailing_stops
    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500)

    result = ensure_trailing_stops(fb)
    assert len(result.submitted) == 1
    o = result.submitted[0]
    assert o.symbol == "SPY"
    assert o.side == "sell"
    assert o.type == "trailing_stop"


def test_ensure_trailing_stops_skips_when_already_attached(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    })

    from orders import ensure_trailing_stops
    from broker import Order
    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500)
    fb.seed_open_order(Order(
        id="existing_trail", symbol="SPY", side="sell", type="trailing_stop",
        qty=10.0, notional=None, status="accepted",
        client_order_id="prev", parent_order_id=None,
    ))

    result = ensure_trailing_stops(fb)
    assert result.submitted == []


def test_ensure_trailing_stops_skips_unknown_tranche(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)  # no cache → position is unknown

    from orders import ensure_trailing_stops
    fb = FakeBroker()
    fb.seed_position("NVDA", qty=5, avg_entry=100)

    result = ensure_trailing_stops(fb)
    assert result.submitted == []


def test_ensure_trailing_stops_respects_halt(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    })
    (tmp_path / "HALT").write_text("paused")

    from orders import ensure_trailing_stops
    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500)

    result = ensure_trailing_stops(fb)
    assert result.submitted == []


# ── OrderIntent new fields (Task 2: intraday execution layer) ────

def test_order_intent_accepts_new_fields():
    i = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="test", tranche="core", client_order_id="x-1",
        stop_pct=0.08, trail_pct=0.12,
        tier="HIGH", decision_price=480.0, max_price=482.4, slice_count=2,
    )
    assert i.tier == "HIGH"
    assert i.decision_price == 480.0
    assert i.max_price == 482.4
    assert i.slice_count == 2


def test_order_intent_new_fields_default_to_none():
    # Backwards-compatible construction (existing paths don't set the new fields).
    i = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="test", tranche="core", client_order_id="x-2",
    )
    assert i.tier is None
    assert i.decision_price is None
    assert i.max_price is None
    assert i.slice_count is None


def test_submit_limit_slice_respects_halt(tmp_path, monkeypatch):
    import orders
    from orders import submit_limit_slice
    from tests.fakes import FakeBroker

    halt_path = tmp_path / "HALT"
    halt_path.write_text("")
    monkeypatch.setattr(orders, "HALT_PATH", str(halt_path))

    b = FakeBroker()
    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="slice", tranche="core", client_order_id="slice-1",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )
    result = submit_limit_slice(intent, limit_price=480.50, notional=250.0, broker=b)
    assert result.submitted == []
    assert any("HALT" in msg for _, msg in result.skipped)


def test_submit_limit_slice_respects_market_closed(monkeypatch, tmp_path):
    import orders
    from orders import submit_limit_slice
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    b = FakeBroker(market_open=False)
    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="slice", tranche="core", client_order_id="slice-2",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )
    result = submit_limit_slice(intent, limit_price=480.50, notional=250.0, broker=b)
    assert result.submitted == []
    assert any("market closed" in msg.lower() for _, msg in result.skipped)


def test_submit_limit_slice_counts_against_daily_cap(monkeypatch, tmp_path):
    import orders
    from orders import submit_limit_slice
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "DAILY_MAX_ORDERS", 1)

    b = FakeBroker()
    b.set_latest_price("SPY", 480.0)
    intent = OrderIntent(
        symbol="SPY", notional=500.0, side="buy",
        reason="slice", tranche="core", client_order_id="slice-3",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=2,
    )
    r1 = submit_limit_slice(intent, limit_price=480.50, notional=250.0, broker=b)
    assert len(r1.submitted) == 1
    intent2 = OrderIntent(
        symbol="SPY", notional=500.0, side="buy",
        reason="slice", tranche="core", client_order_id="slice-3b",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=2,
    )
    r2 = submit_limit_slice(intent2, limit_price=480.60, notional=250.0, broker=b)
    assert len(r2.submitted) == 0
    assert len(r2.deferred) == 1


# ── _effective_stop_pct (ATR-scaled core stops) ─────────────────

def _ohlcv_constant(symbol: str, high: float, low: float, close: float, n: int = 30):
    """Build a MultiIndex OHLCV frame in the shape data.fetch_ohlcv returns."""
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        ("High",  symbol): [high]  * n,
        ("Low",   symbol): [low]   * n,
        ("Close", symbol): [close] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_effective_stop_pct_uses_atr_when_tighter(monkeypatch):
    """Low-vol data (ATR pct < base) → returns ATR-scaled pct."""
    from orders import _effective_stop_pct
    # high=100.5, low=99.5 → TR≈1, last_close=100 → ATR/close=0.01 → 2*ATR/close=0.02
    df = _ohlcv_constant("AAPL", high=100.5, low=99.5, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    # Expect 0.02 (< STOP_LOSS_PCT of 0.08 in balanced mode default)
    assert abs(result - 0.02) < 1e-6


def test_effective_stop_pct_caps_at_base(monkeypatch):
    """High-vol data (ATR pct > base) → returns STOP_LOSS_PCT."""
    import config
    from orders import _effective_stop_pct
    # TR≈20 on a $100 close → 2*ATR/close = 0.40 (> any base)
    df = _ohlcv_constant("TSLA", high=110.0, low=90.0, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("TSLA", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_aggressive_unchanged(monkeypatch):
    """Aggressive tranche short-circuits — does not call fetch_ohlcv."""
    import config
    from orders import _effective_stop_pct

    called = {"hit": False}
    def _trap(*a, **kw):
        called["hit"] = True
        raise AssertionError("fetch_ohlcv must not be called for aggressive")
    monkeypatch.setattr("data.fetch_ohlcv", _trap)

    result = _effective_stop_pct("TQQQ", "aggressive")
    assert result == config.AGGRESSIVE_PARAMS["stop_loss_pct"]
    assert called["hit"] is False


def test_effective_stop_pct_fallback_on_fetch_error(monkeypatch):
    """fetch_ohlcv raising → returns base, no exception escapes."""
    import config
    from orders import _effective_stop_pct

    def _boom(*a, **kw):
        raise RuntimeError("yfinance unavailable")
    monkeypatch.setattr("data.fetch_ohlcv", _boom)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_insufficient_data(monkeypatch):
    """Too few bars for ATR → returns base."""
    import config
    from orders import _effective_stop_pct
    df = _ohlcv_constant("AAPL", high=100.5, low=99.5, close=100.0, n=5)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_zero_atr(monkeypatch):
    """Constant prices → ATR=0 → fallback to base (not 0)."""
    import config
    from orders import _effective_stop_pct
    df = _ohlcv_constant("SHV", high=100.0, low=100.0, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("SHV", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_reconcile_buy_uses_effective_stop(tmp_path, monkeypatch):
    """Buy intent's stop_pct comes from _effective_stop_pct, not _tranche_stops."""
    from orders import reconcile_to_targets

    # Force _effective_stop_pct to return a recognizable value.
    monkeypatch.setattr("orders._effective_stop_pct",
                        lambda sym, tranche: 0.037 if tranche == "core" else 0.10)

    snap = _snap(positions=[], cash=90_000, equity=90_000)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core",
        snapshot=snap,
        tranche_capital=90_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 1
    assert plan.buys[0].symbol == "SPY"
    assert abs(plan.buys[0].stop_pct - 0.037) < 1e-9
    # trail_pct unchanged: still from _tranche_stops("core")
    import config
    assert abs(plan.buys[0].trail_pct - config.TRAILING_STOP_PCT) < 1e-9


def test_reconcile_aggressive_buy_uses_fixed_stop(tmp_path, monkeypatch):
    """Aggressive tranche keeps the fixed stop_loss_pct."""
    from orders import reconcile_to_targets
    import config

    snap = _snap(positions=[], cash=10_000, equity=10_000)
    plan = reconcile_to_targets(
        {"TQQQ": 1.0},
        tranche="aggressive",
        snapshot=snap,
        tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 1
    assert abs(plan.buys[0].stop_pct
               - config.AGGRESSIVE_PARAMS["stop_loss_pct"]) < 1e-9


# ── sync_state SEPA fields ──────────────────────────────────────

def _seed_stop_order(fb, symbol: str, stop_price: float, qty: float = 30.0,
                     parent_id: str = "parent_1"):
    """Attach a fake bracket stop-loss leg for symbol."""
    from broker import Order
    fb.seed_open_order(Order(
        id=f"stop_{symbol}", symbol=symbol, side="sell", type="stop",
        qty=qty, notional=None, status="accepted",
        client_order_id=f"stop-cid-{symbol}", parent_order_id=parent_id,
        stop_price=stop_price,
    ))


def test_sync_state_snapshots_initial_fields_on_first_seen_core_position(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30.0
    assert p["initial_stop_price"] == 92.0
    assert p["r_tier_filled"] == []


def test_sync_state_preserves_initial_fields_across_runs(tmp_path, monkeypatch):
    """Once snapshotted, initial_* fields are never re-written."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    # avg_entry has drifted to 105 (added to position), stop replaced at 95.
    fb.seed_position("AAPL", qty=40, avg_entry=105.0, mv=4200.0)
    _seed_stop_order(fb, "AAPL", stop_price=95.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    # Initial fields are immutable:
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30
    assert p["initial_stop_price"] == 92.0


def test_sync_state_initial_stop_none_when_no_open_stop_order(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    # No stop order seeded.

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30.0
    assert p["initial_stop_price"] is None
    assert p["r_tier_filled"] == []


def test_sync_state_appends_r_tier_when_qty_drops_to_two_thirds(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=20, avg_entry=100.0, mv=2400.0)  # 2/3 of 30
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R"]


def test_sync_state_appends_r_tier_3R_when_qty_drops_to_one_third(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 20.0, "avg_entry": 100.0,
             "market_value": 2400.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": ["2R"]},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=10, avg_entry=100.0, mv=1200.0)  # 1/3 of 30
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R", "3R"]


def test_sync_state_appends_both_tiers_when_qty_drops_in_one_step(tmp_path, monkeypatch):
    """Gap-up partial-sell scenario: r_tier_filled went [] → ["2R", "3R"] in one sync."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=10, avg_entry=100.0, mv=1200.0)  # straight to 1/3
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R", "3R"]


def test_sync_state_does_not_append_r_tier_on_full_qty(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)  # unchanged
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == []


# ── submit_partial_exit ─────────────────────────────────────────

def _seed_cache_position(tmp_path, monkeypatch, symbol="AAPL", initial_qty=30,
                         current_qty=30, avg_entry=100.0, mv=3000.0,
                         r_tier_filled=None, initial_entry_price=100.0,
                         initial_stop_price=92.0):
    """Seed portfolio.json with one SEPA-ready position."""
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 5000.0, "equity": 50_000.0,
        "positions": [{
            "symbol": symbol, "shares": current_qty, "avg_entry": avg_entry,
            "market_value": mv, "unrealized_pl": 0.0,
            "tranche": "core", "entry_reason": "core rebalance",
            "stop_order_id": None, "trail_order_id": None,
            "initial_entry_price": initial_entry_price,
            "initial_qty": initial_qty,
            "initial_stop_price": initial_stop_price,
            "r_tier_filled": r_tier_filled if r_tier_filled is not None else [],
        }],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })


def test_submit_partial_exit_writes_to_pending_plan(tmp_path, monkeypatch):
    from orders import submit_partial_exit
    from pending_plan import load_plan

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    # Stub baseline.capture_baseline so we don't fetch live market data.
    from pending_plan import Baseline
    monkeypatch.setattr("baseline.capture_baseline",
                        lambda: Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                                         news_cursor_at=dt.datetime(2026, 5, 10, 14, 0, 0, tzinfo=dt.timezone.utc)))

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)

    assert len(result.queued) == 1
    intent = result.queued[0]
    assert intent.symbol == "AAPL"
    assert intent.side == "sell"
    # notional ≈ initial_qty * fraction * current_price = 30 * 1/3 * 116 = 1160
    assert abs(intent.notional - 1160.0) < 0.01
    assert intent.reason == "sepa-2R"
    assert intent.tier == "HIGH"

    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and s.intent.reason == "sepa-2R"
               for s in plan.intents)


def test_submit_partial_exit_conflict_falls_back_to_direct(tmp_path, monkeypatch):
    """If pending_plan already has an AAPL intent, submit_partial_exit
    routes through execute_plan (the same pattern as submit_exit)."""
    from orders import submit_partial_exit
    from pending_plan import PendingPlan, IntentState, write_plan, Baseline

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    # Pre-seed pending_plan with a conflicting AAPL intent.
    write_plan(PendingPlan(
        plan_id="conflict-1",
        tranche="core",
        created_at=dt.datetime(2026, 5, 10, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                          news_cursor_at=dt.datetime(2026, 5, 10, 14, 0, 0, tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="AAPL", notional=1500.0, side="sell",
            reason="rebalance sell", tranche="core",
            client_order_id="cid-conflict-1",
        ))],
    ))

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    # Direct execute_plan path → result.submitted has the sell.
    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "AAPL"
    assert result.submitted[0].side == "sell"


def test_submit_partial_exit_skips_when_initial_qty_missing(tmp_path, monkeypatch):
    from orders import submit_partial_exit
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch, initial_qty=None)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert len(result.skipped) == 1


def test_submit_partial_exit_respects_halt(tmp_path, monkeypatch):
    from orders import submit_partial_exit

    halt = tmp_path / "HALT"
    halt.write_text("paused")
    monkeypatch.setattr("orders.HALT_PATH", str(halt))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert any("HALT" in msg for _, msg in result.skipped)


# ── cancel_position_trailing ───────────────────────────────────

def test_cancel_position_trailing_cancels_open_trailing_order(tmp_path, monkeypatch):
    from orders import cancel_position_trailing
    from broker import Order

    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    fb = FakeBroker()
    fb.seed_open_order(Order(
        id="ord_trail_1", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-cid", parent_order_id=None,
    ))
    # Also a stop order — should NOT be cancelled.
    fb.seed_open_order(Order(
        id="ord_stop_1", symbol="AAPL", side="sell", type="stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="stop-cid", parent_order_id=None, stop_price=92.0,
    ))

    result = cancel_position_trailing("AAPL", broker=fb)
    assert "ord_trail_1" in fb._canceled
    assert "ord_stop_1" not in fb._canceled
    assert result.skipped == []


def test_cancel_position_trailing_noop_when_no_trailing(tmp_path, monkeypatch):
    from orders import cancel_position_trailing

    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    fb = FakeBroker()
    result = cancel_position_trailing("AAPL", broker=fb)
    assert fb._canceled == []
    assert result.submitted == []
    assert result.skipped == []


def test_cancel_position_trailing_respects_halt(tmp_path, monkeypatch):
    from orders import cancel_position_trailing
    from broker import Order

    halt = tmp_path / "HALT"
    halt.write_text("paused")
    monkeypatch.setattr("orders.HALT_PATH", str(halt))

    fb = FakeBroker()
    fb.seed_open_order(Order(
        id="ord_trail_1", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-cid", parent_order_id=None,
    ))

    result = cancel_position_trailing("AAPL", broker=fb)
    assert fb._canceled == []  # HALT prevented the cancel
    assert any("HALT" in msg for _, msg in result.skipped)


# ── entry pivots sidecar ─────────────────────────────────────────

def test_load_entry_pivots_missing_file_returns_empty(tmp_path, monkeypatch):
    from orders import _load_entry_pivots
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    assert _load_entry_pivots() == {}


def test_save_then_load_entry_pivots_roundtrip(tmp_path, monkeypatch):
    from orders import _load_entry_pivots, _save_entry_pivots
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    data = {"AAPL": {"pivot": 200.5, "entry_date": "2026-05-18"}}
    _save_entry_pivots(data)
    assert _load_entry_pivots() == data


def test_load_entry_pivots_malformed_returns_empty(tmp_path, monkeypatch):
    from orders import _load_entry_pivots
    path = tmp_path / "pivots.json"
    path.write_text("not-json")
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(path))
    assert _load_entry_pivots() == {}


# ── sync_state climax_fired ─────────────────────────────────────

def test_sync_state_initializes_climax_fired_false_on_first_seen(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["climax_fired"] is False


def test_sync_state_preserves_climax_fired_across_runs(tmp_path, monkeypatch):
    """Once set to True (by watchdog._set_climax_fired), sync_state preserves it."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
             "market_value": 1500.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": True},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=15, avg_entry=100.0, mv=1500.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["climax_fired"] is True


def test_sync_state_does_not_append_r_tier_when_climax_fired_true(tmp_path, monkeypatch):
    """Climax sold 50%; qty drop must NOT trigger r_tier 2R/3R appends."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": True},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=15, avg_entry=100.0, mv=1500.0)  # climax sold half
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    # Without the gate, r_tier_filled would falsely contain "2R" (and maybe "3R").
    assert p["r_tier_filled"] == []
    assert p["climax_fired"] is True


# ── cash-aware gate ─────────────────────────────────────────────

def test_execute_plan_rejects_buys_when_cash_insufficient(tmp_path, monkeypatch):
    """Total buy notional > broker.cash → buys skipped, sells still go through."""
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=15_000, side="buy",
                          reason="test", tranche="core",
                          client_order_id="buy-cid",
                          stop_pct=0.08, trail_pct=0.12)],
        sells=[OrderIntent(symbol="QQQ", notional=2_000, side="sell",
                           reason="test", tranche="core",
                           client_order_id="sell-cid")],
        holds=[],
    )
    fb = FakeBroker(cash=10_000.0)
    fb.seed_position("QQQ", qty=5, avg_entry=400, mv=2000)

    result = execute_plan(plan, broker=fb, reason="test")

    # Buy skipped with cash-aware reason
    buy_skips = [s for s in result.skipped if s[0].side == "buy"]
    assert len(buy_skips) == 1
    assert "cash-aware" in buy_skips[0][1]
    assert "$15,000" in buy_skips[0][1]
    # Sell unaffected
    assert any(o.side == "sell" for o in result.submitted)


def test_execute_plan_allows_buys_when_cash_sufficient(tmp_path, monkeypatch):
    """Total buy notional ≤ broker.cash → buys submitted."""
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=5_000, side="buy",
                          reason="test", tranche="core",
                          client_order_id="buy-cid-1",
                          stop_pct=0.08, trail_pct=0.12)],
        sells=[], holds=[],
    )
    fb = FakeBroker(cash=20_000.0)

    result = execute_plan(plan, broker=fb, reason="test")

    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "SPY"
    assert not any("cash-aware" in s[1] for s in result.skipped)


def test_execute_plan_allows_margin_when_config_flag_true(tmp_path, monkeypatch):
    """ALLOW_MARGIN=True bypasses the cash-aware gate."""
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("config.ALLOW_MARGIN", True)

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=15_000, side="buy",
                          reason="test", tranche="core",
                          client_order_id="buy-cid-2",
                          stop_pct=0.08, trail_pct=0.12)],
        sells=[], holds=[],
    )
    fb = FakeBroker(cash=10_000.0)  # would normally reject

    result = execute_plan(plan, broker=fb, reason="test")

    assert len(result.submitted) == 1
    assert not any("cash-aware" in s[1] for s in result.skipped)


def test_execute_plan_rejects_buys_when_account_fetch_fails(tmp_path, monkeypatch):
    """Broker account fetch raises → defensive default rejects all buys."""
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=5_000, side="buy",
                          reason="test", tranche="core",
                          client_order_id="buy-cid-3",
                          stop_pct=0.08, trail_pct=0.12)],
        sells=[], holds=[],
    )
    fb = FakeBroker(cash=20_000.0)
    monkeypatch.setattr(fb, "get_account", lambda: (_ for _ in ()).throw(
        __import__("broker").BrokerError("account fetch failed")))

    result = execute_plan(plan, broker=fb, reason="test")

    assert result.submitted == []
    assert len(result.skipped) == 1
    assert "broker account fetch failed" in result.skipped[0][1]


def test_execute_plan_cash_aware_does_not_affect_sell_only_plan(tmp_path, monkeypatch):
    """A plan with only sells doesn't trigger get_account."""
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[],
        sells=[OrderIntent(symbol="QQQ", notional=2_000, side="sell",
                           reason="test", tranche="core",
                           client_order_id="sell-cid-2")],
        holds=[],
    )
    fb = FakeBroker(cash=0.0)  # cash exhausted, but no buys, so irrelevant
    fb.seed_position("QQQ", qty=5, avg_entry=400, mv=2000)

    result = execute_plan(plan, broker=fb, reason="test")
    assert any(o.side == "sell" for o in result.submitted)
    assert not any("cash-aware" in s[1] for s in result.skipped)
