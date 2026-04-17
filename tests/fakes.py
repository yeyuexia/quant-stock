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

    _positions: dict[str, Position] = field(default_factory=dict)
    _open_orders: list[Order] = field(default_factory=list)
    _submitted: list[Order] = field(default_factory=list)
    _seen_cids: set[str] = field(default_factory=set)
    _id_gen: itertools.count = field(default_factory=lambda: itertools.count(1))
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

    def submit_market(self, symbol, *, notional=None, qty=None, side, client_order_id):
        return self._submit(symbol, notional=notional, qty=qty, side=side,
                             cid=client_order_id, type_="market")

    def submit_bracket(self, symbol, *, notional, stop_loss_pct, trailing_stop_pct, client_order_id):
        return self._submit(symbol, notional=notional, qty=None, side="buy",
                             cid=client_order_id, type_="bracket")

    def submit_trailing_stop(self, symbol, *, qty, trail_percent, client_order_id):
        return self._submit(symbol, notional=None, qty=qty, side="sell",
                             cid=client_order_id, type_="trailing_stop")

    def cancel_order(self, order_id: str) -> None:
        self._open_orders = [o for o in self._open_orders if o.id != order_id]

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
