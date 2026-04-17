"""
Alpaca broker wrapper. Pure I/O — no policy, no retries, no caching.
Callers must be `orders.py` or tests only.

All return types are plain dataclasses; no alpaca-py objects leak out.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional


class BrokerError(Exception):
    """Raised on any Alpaca API failure or contract violation."""


class ConfigError(Exception):
    """Raised when Broker is constructed without the required env vars."""


@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    equity: float
    buying_power: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    side: str                       # "buy" | "sell"
    type: str                       # "market" | "stop" | "trailing_stop" | "bracket"
    qty: Optional[float]
    notional: Optional[float]
    status: str                     # "accepted" | "filled" | "canceled" | ...
    client_order_id: str
    parent_order_id: Optional[str]  # for legs of a bracket order


from alpaca.trading.client import TradingClient


class Broker:
    """Thin wrapper around alpaca-py TradingClient.

    Use `env="paper"` (default) for the simulated account.
    `env="live"` additionally requires ALPACA_LIVE_CONFIRM=yes in the environment.
    """

    def __init__(self, env: str = "paper"):
        if env not in ("paper", "live"):
            raise ConfigError(f"env must be 'paper' or 'live', got {env!r}")

        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        if not key or not secret:
            raise ConfigError(
                "ALPACA_API_KEY and ALPACA_API_SECRET must be set in the environment"
            )

        if env == "live" and os.environ.get("ALPACA_LIVE_CONFIRM") != "yes":
            raise ConfigError(
                "Live trading requires ALPACA_LIVE_CONFIRM=yes. "
                "Refusing to construct a live Broker."
            )

        self.env = env
        self._client = TradingClient(
            api_key=key,
            secret_key=secret,
            paper=(env == "paper"),
        )

    def get_account(self) -> AccountSnapshot:
        try:
            a = self._client.get_account()
        except Exception as e:
            raise BrokerError(f"get_account failed: {e}") from e
        return AccountSnapshot(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
        except Exception as e:
            raise BrokerError(f"get_positions failed: {e}") from e
        return [
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in raw
        ]

    def get_open_orders(self) -> list[Order]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        try:
            raw = self._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
            )
        except Exception as e:
            raise BrokerError(f"get_open_orders failed: {e}") from e
        out: list[Order] = []
        for o in raw:
            out.append(_to_order(o))
            # Bracket "legs" (stop, trailing) come nested under the parent.
            for leg in (getattr(o, "legs", None) or []):
                out.append(_to_order(leg, parent_id=o.id))
        return out

    def is_market_open(self) -> bool:
        try:
            return bool(self._client.get_clock().is_open)
        except Exception as e:
            raise BrokerError(f"is_market_open failed: {e}") from e


def _to_order(o, parent_id: Optional[str] = None) -> Order:
    return Order(
        id=str(o.id),
        symbol=o.symbol,
        side=str(o.side.value if hasattr(o.side, "value") else o.side).lower(),
        type=str(o.type.value if hasattr(o.type, "value") else o.type).lower(),
        qty=float(o.qty) if o.qty is not None else None,
        notional=float(o.notional) if getattr(o, "notional", None) is not None else None,
        status=str(o.status.value if hasattr(o.status, "value") else o.status).lower(),
        client_order_id=o.client_order_id or "",
        parent_order_id=parent_id,
    )
