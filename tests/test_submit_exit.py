# tests/test_submit_exit.py
import datetime as dt
import json
from tests.fakes import FakeBroker


def test_submit_exit_writes_to_pending_plan(tmp_path, monkeypatch):
    import quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "MACRO_EXIT_TOLERANCE_BPS", 150)

    with open(orders.PORTFOLIO_PATH, "w") as f:
        json.dump({
            "positions": [{
                "symbol": "TQQQ", "shares": 100, "avg_entry": 60.0,
                "market_value": 6000.0, "unrealized_pl": 0.0,
                "tranche": "aggressive", "entry_reason": "aggressive rebalance",
            }],
            "tranches": {"core": {"last_rebalance": None},
                         "aggressive": {"last_rebalance": None}},
        }, f)

    import quant.signals.baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 18.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: -0.25)

    b = FakeBroker()
    b.set_latest_price("TQQQ", 58.0)

    orders.submit_exit("TQQQ", reason="macro contraction", broker=b)

    plan = load_plan()
    assert plan is not None
    assert len(plan.intents) == 1
    state = plan.intents[0]
    assert state.intent.symbol == "TQQQ"
    assert state.intent.side == "sell"
    assert state.intent.tier == "HIGH"
    # 150 bps floor: 58 × (1 - 0.015) = 57.13
    assert round(state.intent.max_price, 2) == round(58.0 * (1 - 0.015), 2)
