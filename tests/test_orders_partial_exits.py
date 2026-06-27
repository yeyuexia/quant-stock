"""Tests for orders.submit_partial_exit — SEPA R-tier partial sells.

Extracted from test_orders.py to give this focused, multi-test surface its
own file. cancel_position_trailing and submit_exit stay in test_orders.py
since they are smaller surfaces."""
import datetime as dt
import json

from quant.execution.orders import OrderIntent
from tests.fakes import FakeBroker


def _portfolio_cache(tmp_path, monkeypatch, data):
    """Point PORTFOLIO_PATH/etc at tmp dir and seed portfolio.json.

    Duplicate of the helper in test_orders.py — kept local so this file is
    self-contained (the helper is trivial enough that a shared helpers
    module would be more friction than the duplication)."""
    monkeypatch.setattr("quant.execution.orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("quant.execution.orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


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
    from quant.execution.orders import submit_partial_exit
    from quant.execution.pending_plan import load_plan

    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("quant.execution.orders.HALT_PATH", str(tmp_path / "no_halt"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    # Stub baseline.capture_baseline so we don't fetch live market data.
    from quant.execution.pending_plan import Baseline
    monkeypatch.setattr("quant.signals.baseline.capture_baseline",
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
    from quant.execution.orders import submit_partial_exit
    from quant.execution.pending_plan import PendingPlan, IntentState, write_plan, Baseline

    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("quant.execution.orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
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
    from quant.execution.orders import submit_partial_exit
    monkeypatch.setattr("quant.execution.orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch, initial_qty=None)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert len(result.skipped) == 1


def test_submit_partial_exit_respects_halt(tmp_path, monkeypatch):
    from quant.execution.orders import submit_partial_exit

    halt = tmp_path / "HALT"
    halt.write_text("paused")
    monkeypatch.setattr("quant.execution.orders.HALT_PATH", str(halt))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert any("HALT" in msg for _, msg in result.skipped)
