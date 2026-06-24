"""Broker construction + live-confirm guard."""
import os
import pytest
from broker import Broker, ConfigError
from broker import Broker, BrokerError, ConfigError, AccountSnapshot
from unittest.mock import MagicMock, patch
import sys
import time


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


def test_order_dataclass_has_stop_price_field():
    """broker.Order exposes stop_price for stop orders."""
    from broker import Order
    o = Order(
        id="ord_1", symbol="AAPL", side="sell", type="stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="cid", parent_order_id="parent_1",
        stop_price=92.0,
    )
    assert o.stop_price == 92.0


def test_order_stop_price_defaults_to_none():
    """stop_price is optional with None default for non-stop orders."""
    from broker import Order
    o = Order(
        id="ord_2", symbol="AAPL", side="buy", type="market",
        qty=None, notional=1000.0, status="accepted",
        client_order_id="cid2", parent_order_id=None,
    )
    assert o.stop_price is None


# ======================================================================
# Post-review additions (formerly test_broker_optimizations.py)
# ======================================================================

"""Regression tests for broker.py optimizations (B1, B2, B3, B4, B5, B6, B13).

These test the contract changes, not the alpaca-py SDK itself."""
import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from broker import Broker, BrokerError, ConfigError, AccountSnapshot


def _make_broker(monkeypatch, env="paper"):
    """Build a Broker without touching alpaca-py real init (stub the clients)."""
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test_secret")
    if env == "live":
        monkeypatch.setenv("ALPACA_LIVE_CONFIRM", "yes")
    fake_trading = MagicMock()
    fake_md = MagicMock()
    with patch("broker.TradingClient", return_value=fake_trading), \
         patch("alpaca.data.historical.StockHistoricalDataClient",
               return_value=fake_md):
        b = Broker(env=env)
    return b, fake_trading, fake_md


# ── B1: md client is constructed exactly once per Broker ───────────

def test_md_client_constructed_only_in_init(monkeypatch):
    """latest_quote + latest_price must reuse self._md_client, not new ones."""
    b, _trading, fake_md = _make_broker(monkeypatch)

    # Stub the data responses
    fake_q = MagicMock()
    fake_q.bid_price = 99.5
    fake_q.ask_price = 100.5
    fake_md.get_stock_latest_quote.return_value = {"NVDA": fake_q}

    fake_t = MagicMock()
    fake_t.price = 100.0
    fake_md.get_stock_latest_trade.return_value = {"NVDA": fake_t}

    # Call both methods multiple times
    for _ in range(5):
        b.latest_quote("NVDA")
        b.latest_price("NVDA")

    # Both used the SAME md client (same MagicMock instance)
    assert fake_md.get_stock_latest_quote.call_count == 5
    assert fake_md.get_stock_latest_trade.call_count == 5


# ── B5: close_all_positions blocks live ─────────────────────────────

def test_close_all_positions_blocked_on_live(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch, env="live")
    with pytest.raises(BrokerError, match="blocked on live"):
        b.close_all_positions()
    fake_trading.close_all_positions.assert_not_called()


def test_close_all_positions_works_on_paper(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch, env="paper")
    b.close_all_positions()
    fake_trading.close_all_positions.assert_called_once_with(cancel_orders=True)


# ── B6: latest_price + _latest_price alias ──────────────────────────

def test_latest_price_alias_still_works(monkeypatch):
    b, _trading, fake_md = _make_broker(monkeypatch)
    fake_t = MagicMock()
    fake_t.price = 150.0
    fake_md.get_stock_latest_trade.return_value = {"AAPL": fake_t}
    # Both names should work (alias)
    assert b.latest_price("AAPL") == 150.0
    assert b._latest_price("AAPL") == 150.0


# ── B2+B9: submit_bracket takes absolute stop_price ─────────────────

def test_submit_bracket_uses_passed_stop_price(monkeypatch):
    """broker.submit_bracket must NOT call latest_price internally — caller
    is responsible for computing stop_price in dollars."""
    b, fake_trading, fake_md = _make_broker(monkeypatch)
    fake_trading.submit_order.return_value = _fake_alpaca_order(
        id="ord1", symbol="NVDA", side="buy", type="market",
        client_order_id="cid-test",
    )

    b.submit_bracket("NVDA", notional=1000.0, stop_price=92.0,
                     client_order_id="cid-test")

    # The MarketOrderRequest passed to submit_order must have stop_price=92.0
    call_args = fake_trading.submit_order.call_args
    req = call_args.args[0] if call_args.args else call_args.kwargs["order_data"]
    assert req.stop_loss.stop_price == 92.0
    # latest_price MUST NOT have been called (no longer in broker's job)
    fake_md.get_stock_latest_trade.assert_not_called()


def test_submit_bracket_take_profit_uses_unreachable_default(monkeypatch):
    """Default take_profit must be unreachable (1e6 × stop_price) so a real
    rally doesn't accidentally trigger it."""
    b, fake_trading, _ = _make_broker(monkeypatch)
    fake_trading.submit_order.return_value = _fake_alpaca_order(
        id="ord1", symbol="NVDA", side="buy", type="market",
        client_order_id="cid-test",
    )

    b.submit_bracket("NVDA", notional=1000.0, stop_price=100.0,
                     client_order_id="cid-test")
    req = fake_trading.submit_order.call_args.args[0]
    # tp_price = 100 × 1_000_000 = 100M; far above any reasonable price.
    assert req.take_profit.limit_price >= 1_000_000


def test_submit_bracket_rejects_zero_stop(monkeypatch):
    b, _trading, _ = _make_broker(monkeypatch)
    with pytest.raises(BrokerError, match="non-positive stop_price"):
        b.submit_bracket("NVDA", notional=1000.0, stop_price=0,
                         client_order_id="cid")
    with pytest.raises(BrokerError, match="non-positive stop_price"):
        b.submit_bracket("NVDA", notional=1000.0, stop_price=-1,
                         client_order_id="cid")


# ── B3+B10: get_filled_notional returns None on query failure ──────

def test_get_filled_notional_returns_none_on_query_failure(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch)
    fake_trading.get_order_by_client_order_id.side_effect = RuntimeError("blip")
    assert b.get_filled_notional("cid-x") is None


def test_get_filled_notional_returns_zero_on_unfilled(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch)
    fake_o = MagicMock()
    fake_o.filled_qty = "0"
    fake_o.filled_avg_price = None
    fake_trading.get_order_by_client_order_id.return_value = fake_o
    # filled_avg_price is None → returns 0.0
    assert b.get_filled_notional("cid-x") == 0.0


def test_get_filled_notional_returns_product_when_filled(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch)
    fake_o = MagicMock()
    fake_o.filled_qty = "10"
    fake_o.filled_avg_price = "150.5"
    fake_trading.get_order_by_client_order_id.return_value = fake_o
    assert b.get_filled_notional("cid-x") == 1505.0


# ── B13: is_market_open caches result ──────────────────────────────

def test_is_market_open_caches_within_ttl(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch)
    clock_obj = MagicMock()
    clock_obj.is_open = True
    fake_trading.get_clock.return_value = clock_obj

    # 5 consecutive calls within TTL — only ONE Alpaca call
    for _ in range(5):
        assert b.is_market_open() is True
    assert fake_trading.get_clock.call_count == 1


def test_is_market_open_refreshes_after_ttl(monkeypatch):
    b, fake_trading, _ = _make_broker(monkeypatch)
    b._market_open_ttl = 0.01   # tiny TTL for the test
    clock_obj = MagicMock()
    clock_obj.is_open = True
    fake_trading.get_clock.return_value = clock_obj

    b.is_market_open()
    time.sleep(0.02)
    b.is_market_open()
    assert fake_trading.get_clock.call_count == 2


# ── B2 caller side: orders._submit_intent fetches price + passes stop_price ──

def test_orders_submit_intent_fetches_price_and_passes_stop_price(tmp_path, monkeypatch):
    """Verify the policy↔IO contract: orders.py fetches latest_price and
    computes stop_price in dollars, broker.submit_bracket just submits."""
    import orders
    from orders import OrderIntent, ExecutionResult
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))

    fb = FakeBroker()
    fb.set_latest_price("NVDA", 200.0)

    captured = {}
    original_submit_bracket = fb.submit_bracket
    def spy(symbol, *, notional, stop_price, client_order_id, take_profit_price=None):
        captured["stop_price"] = stop_price
        return original_submit_bracket(
            symbol, notional=notional, stop_price=stop_price,
            client_order_id=client_order_id,
            take_profit_price=take_profit_price,
        )
    monkeypatch.setattr(fb, "submit_bracket", spy)

    intent = OrderIntent(
        symbol="NVDA", notional=1000.0, side="buy",
        reason="test", tranche="core", client_order_id="cid-test",
        stop_pct=0.08, trail_pct=0.12,
    )
    result = ExecutionResult()
    orders._submit_intent(fb, intent, result)

    # stop_price = 200 × (1 - 0.08) = 184.0
    assert captured["stop_price"] == 184.0


# ── helpers ─────────────────────────────────────────────────────────

def _fake_alpaca_order(**kw):
    """Build a MagicMock that looks like an alpaca-py Order."""
    m = MagicMock()
    m.id = kw["id"]
    m.symbol = kw["symbol"]
    m.side = kw["side"]
    m.type = kw["type"]
    m.qty = kw.get("qty")
    m.notional = kw.get("notional")
    m.status = kw.get("status", "accepted")
    m.client_order_id = kw.get("client_order_id", "")
    m.stop_price = kw.get("stop_price")
    m.legs = None
    return m
