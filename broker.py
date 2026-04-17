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
