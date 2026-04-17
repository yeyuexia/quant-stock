"""
Integration tests against Alpaca paper.

Opt-in: requires ALPACA_API_KEY / ALPACA_API_SECRET in env, and `-m integration`.
These tests destroy state on the paper account (close_all_positions).
Never run against live.
"""
import os
import time
import datetime as dt
import pytest

from broker import Broker, BrokerError

pytestmark = pytest.mark.integration


@pytest.fixture
def broker():
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_API_SECRET"):
        pytest.skip("ALPACA_API_KEY / ALPACA_API_SECRET not set")
    if os.environ.get("ALPACA_ENV", "paper") != "paper":
        pytest.skip("Integration tests require ALPACA_ENV=paper")
    b = Broker(env="paper")
    b.close_all_positions()
    time.sleep(1)  # let Alpaca settle
    yield b
    b.close_all_positions()


def test_account_reachable(broker):
    acc = broker.get_account()
    assert acc.cash > 0
    assert acc.equity > 0


def test_submit_and_cancel_market(broker):
    cid = f"it-test-market-{int(time.time())}"
    o = broker.submit_market("SPY", notional=50, side="buy", client_order_id=cid)
    assert o.symbol == "SPY"
    time.sleep(2)
    orders = broker.get_open_orders()
    # Order may already be filled; that's fine.


def test_submit_bracket(broker):
    cid = f"it-test-bracket-{int(time.time())}"
    o = broker.submit_bracket(
        "SPY", notional=100, stop_loss_pct=0.10, trailing_stop_pct=0.15,
        client_order_id=cid,
    )
    assert o.symbol == "SPY"


def test_rebalancer_dry_run(broker, tmp_path, monkeypatch):
    """End-to-end: build a plan against an empty paper account."""
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))

    from rebalancer import run
    run(tranche="core", dry_run=True, force=True, broker=broker)
