"""Test doubles for broker and clock.

FakeBroker satisfies the same interface as broker.Broker but stores state
in memory. Lets us unit-test orders.py without hitting the network.
"""
from __future__ import annotations
import itertools
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from broker import AccountSnapshot, Position, Order, BrokerError


@dataclass
class FakeBroker:
    """In-memory broker. Use `seed_position` / `set_cash` to arrange state."""
    env: str = "paper"
    cash: float = 100_000.0
    equity: float = 100_000.0
    buying_power: float = 100_000.0
    market_open: bool = True
    latest_prices: dict[str, float] = field(default_factory=dict)
    latest_quotes: dict[str, tuple[float, float]] = field(default_factory=dict)
    # Default is None (strict): unseeded symbols raise. Tests that don't
    # care about the specific price can opt into a default with
    # FakeBroker(default_price=100.0). Strict-by-default catches the case
    # where a test passes only because the FakeBroker silently invented a
    # price for a symbol the production code shouldn't have queried.
    default_price: Optional[float] = None

    _positions: dict[str, Position] = field(default_factory=dict)
    _open_orders: list[Order] = field(default_factory=list)
    _submitted: list[Order] = field(default_factory=list)
    _seen_cids: set[str] = field(default_factory=set)
    _id_gen: itertools.count = field(default_factory=lambda: itertools.count(1))
    _canceled: list[str] = field(default_factory=list)
    _fills: dict = field(default_factory=dict)   # client_order_id → filled notional
    _fail_on_submit: Optional[Exception] = None

    # ── arrange helpers ─────────────────────────────────────────
    def seed_position(self, symbol: str, qty: float, avg_entry: float, mv: Optional[float] = None):
        self._positions[symbol] = Position(
            symbol=symbol, qty=qty, avg_entry=avg_entry,
            market_value=mv if mv is not None else qty * avg_entry,
            unrealized_pl=0.0,
        )

    def seed_open_order(self, order: Order):
        self._open_orders.append(order)

    def set_latest_price(self, symbol: str, price: float):
        self.latest_prices[symbol] = price

    def set_latest_quote(self, symbol: str, bid: float, ask: float):
        self.latest_quotes[symbol] = (bid, ask)

    def set_fill(self, client_order_id: str, filled_notional: float):
        self._fills[client_order_id] = filled_notional

    def fail_next_submit(self, exc: Exception):
        self._fail_on_submit = exc

    # ── Broker interface ────────────────────────────────────────
    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=self.cash, equity=self.equity, buying_power=self.buying_power)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_open_orders(self) -> list[Order]:
        return list(self._open_orders)

    def is_market_open(self) -> bool:
        return self.market_open

    def latest_price(self, symbol: str) -> float:
        p = self.latest_prices.get(symbol)
        if p is not None:
            return p
        if self.default_price is None:
            raise BrokerError(f"FakeBroker: no latest price seeded for {symbol}")
        return self.default_price

    # Back-compat alias matching the real Broker — old test callers used
    # the underscored form when it was "private".
    _latest_price = latest_price

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        if symbol in self.latest_quotes:
            return self.latest_quotes[symbol]
        p = self.latest_prices.get(symbol)
        if p is not None:
            return p * 0.999, p * 1.001
        raise BrokerError(f"FakeBroker: no quote seeded for {symbol}")

    def submit_market(self, symbol, *, notional=None, qty=None, side, client_order_id):
        return self._submit(symbol, notional=notional, qty=qty, side=side,
                             cid=client_order_id, type_="market")

    def submit_limit(self, symbol, *, notional=None, qty=None, side, limit_price,
                     client_order_id, time_in_force="day"):
        return self._submit(symbol, notional=notional, qty=qty, side=side,
                             cid=client_order_id, type_="limit")

    def submit_bracket(self, symbol, *, notional, stop_price, client_order_id,
                       take_profit_price=None):
        return self._submit(symbol, notional=notional, qty=None, side="buy",
                             cid=client_order_id, type_="bracket")

    def submit_trailing_stop(self, symbol, *, qty, trail_percent, client_order_id):
        return self._submit(symbol, notional=None, qty=qty, side="sell",
                             cid=client_order_id, type_="trailing_stop")

    def get_filled_notional(self, client_order_id: str) -> Optional[float]:
        # FakeBroker assumes lookups always succeed → returns 0.0 (not None).
        # Tests that want to simulate the broker-query-failed case can
        # override via monkeypatch.
        return self._fills.get(client_order_id, 0.0)

    def cancel_order(self, order_id: str) -> None:
        self._open_orders = [o for o in self._open_orders if o.id != order_id]
        self._canceled.append(order_id)

    def close_all_positions(self) -> None:
        self._positions.clear()
        self._open_orders.clear()

    def _submit(self, symbol, *, notional, qty, side, cid, type_) -> Order:
        if self._fail_on_submit is not None:
            exc, self._fail_on_submit = self._fail_on_submit, None
            raise exc
        if cid in self._seen_cids:
            raise BrokerError(f"duplicate client_order_id: {cid}")
        self._seen_cids.add(cid)
        oid = f"ord_{next(self._id_gen)}"
        order = Order(
            id=oid, symbol=symbol, side=side, type=type_,
            qty=qty, notional=notional, status="accepted",
            client_order_id=cid, parent_order_id=None,
        )
        self._submitted.append(order)
        self._open_orders.append(order)
        return order


@dataclass
class FakeClock:
    """Deterministic clock for time-dependent tests (expiry, daily buckets)."""
    now_value: dt.datetime = field(
        default_factory=lambda: dt.datetime(2026, 4, 17, 14, 0, 0, tzinfo=dt.timezone.utc)
    )

    def now(self) -> dt.datetime:
        return self.now_value

    def advance(self, seconds: float):
        self.now_value = self.now_value + dt.timedelta(seconds=seconds)


@dataclass
class FakeMarketData:
    """Deterministic SPY/VIX/per-symbol prices indexed by simulated clock."""
    spy_by_time: dict = field(default_factory=dict)
    vix_by_time: dict = field(default_factory=dict)
    symbol_prices_by_time: dict = field(default_factory=dict)
    macro_score: float = 0.0

    def spy_at(self, t: dt.datetime) -> float:
        return self.spy_by_time.get(t, 480.0)

    def vix_at(self, t: dt.datetime) -> float:
        return self.vix_by_time.get(t, 14.0)

    def price_at(self, symbol: str, t: dt.datetime) -> float:
        return self.symbol_prices_by_time.get((symbol, t), 100.0)


@dataclass
class FakeNewsFeed:
    """Canned keyword hits, replayable by timestamp."""
    headlines: list = field(default_factory=list)

    def add(self, *, title: str, source: str, ts: dt.datetime):
        self.headlines.append({"title": title, "source": source, "ts": ts})

    def fetch_since(self, since: dt.datetime) -> list:
        return [h for h in self.headlines if h["ts"] >= since]
