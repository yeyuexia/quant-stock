"""Broker construction + live-confirm guard."""
import os
import pytest
from broker import Broker, ConfigError


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("ALPACA_ENV", "ALPACA_LIVE_CONFIRM", "ALPACA_API_KEY", "ALPACA_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_paper_env_constructs_with_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    b = Broker(env="paper")
    assert b.env == "paper"


def test_live_requires_confirm_flag(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    with pytest.raises(ConfigError, match="ALPACA_LIVE_CONFIRM"):
        Broker(env="live")


def test_live_with_confirm_constructs(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setenv("ALPACA_LIVE_CONFIRM", "yes")
    b = Broker(env="live")
    assert b.env == "live"


def test_missing_keys_raises(monkeypatch):
    with pytest.raises(ConfigError, match="ALPACA_API_KEY"):
        Broker(env="paper")


def test_bad_env_raises():
    with pytest.raises(ConfigError, match="env"):
        Broker(env="demo")


def test_submit_limit_constructs_limit_order_request(monkeypatch):
    """broker.submit_limit passes limit_price into LimitOrderRequest."""
    import broker as broker_mod
    captured = {}

    class FakeTradingClient:
        def __init__(self, *a, **kw): pass
        def submit_order(self, req):
            captured["req"] = req
            from types import SimpleNamespace
            return SimpleNamespace(
                id="ord-1", symbol=req.symbol, side=req.side, type="limit",
                qty=req.qty, notional=req.notional, status="accepted",
                client_order_id=req.client_order_id,
            )

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setattr(broker_mod, "TradingClient", FakeTradingClient)
    b = broker_mod.Broker(env="paper")
    out = b.submit_limit("SPY", notional=1000.0, side="buy",
                         limit_price=480.50, client_order_id="cid-1")
    assert captured["req"].limit_price == 480.50
    assert captured["req"].notional == 1000.0
    assert out.symbol == "SPY"
    assert out.type == "limit"
