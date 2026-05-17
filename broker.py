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
    stop_price: Optional[float] = None  # set for stop / stop_loss orders


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

    def submit_market(
        self,
        symbol: str,
        *,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
        side: str,
        client_order_id: str,
    ) -> Order:
        """Submit a plain market order. Exactly one of notional/qty must be set."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if (notional is None) == (qty is None):
            raise BrokerError("submit_market: specify exactly one of notional or qty")
        if side not in ("buy", "sell"):
            raise BrokerError(f"submit_market: invalid side {side!r}")

        req = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        try:
            o = self._client.submit_order(req)
        except Exception as e:
            raise BrokerError(f"submit_market({symbol}) failed: {e}") from e
        return _to_order(o)

    def submit_limit(
        self,
        symbol: str,
        *,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
        side: str,
        limit_price: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> Order:
        """Submit a limit order. Exactly one of notional/qty must be set."""
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if (notional is None) == (qty is None):
            raise BrokerError("submit_limit: specify exactly one of notional or qty")
        if side not in ("buy", "sell"):
            raise BrokerError(f"submit_limit: invalid side {side!r}")
        if limit_price <= 0:
            raise BrokerError(f"submit_limit: limit_price must be positive, got {limit_price}")

        if time_in_force not in ("day", "gtc"):
            raise BrokerError(f"submit_limit: unsupported time_in_force {time_in_force!r}")
        tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC
        req = LimitOrderRequest(
            symbol=symbol,
            notional=notional,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=tif,
            limit_price=limit_price,
            client_order_id=client_order_id,
        )
        try:
            o = self._client.submit_order(req)
        except Exception as e:
            raise BrokerError(f"submit_limit({symbol}) failed: {e}") from e
        return _to_order(o)

    def submit_bracket(
        self,
        symbol: str,
        *,
        notional: float,
        stop_loss_pct: float,
        trailing_stop_pct: float,
        client_order_id: str,
    ) -> Order:
        """Submit a market buy with an OCO stop-loss + trailing-stop attached.

        Alpaca's bracket order requires stop_loss and take_profit legs; we set
        take_profit to a very high limit so only the stop side is active, and
        we issue a *separate* trailing-stop order after the bracket fills.
        This is done by the caller via `orders.py`, not here — this method
        only places the bracket with the fixed stop.
        """
        from alpaca.trading.requests import (
            MarketOrderRequest, StopLossRequest, TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if notional <= 0:
            raise BrokerError(f"submit_bracket: non-positive notional {notional}")
        if not (0 < stop_loss_pct < 1):
            raise BrokerError(f"submit_bracket: stop_loss_pct out of range {stop_loss_pct}")
        if not (0 < trailing_stop_pct < 1):
            raise BrokerError(f"submit_bracket: trailing_stop_pct out of range {trailing_stop_pct}")

        # Bracket requires a stop-loss leg priced in absolute dollars.
        # We use Alpaca's server-side "trail_percent" trailing stop feature
        # via a separate trailing-stop order; here we attach only the hard stop.
        # Compute stop price relative to the current ask (fetched via latest quote).
        last_price = self._latest_price(symbol)
        stop_price = round(last_price * (1 - stop_loss_pct), 2)
        # Take-profit leg is required by Alpaca for bracket; set far above market.
        tp_price = round(last_price * 10, 2)

        req = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=stop_price),
            take_profit=TakeProfitRequest(limit_price=tp_price),
        )
        try:
            o = self._client.submit_order(req)
        except Exception as e:
            raise BrokerError(f"submit_bracket({symbol}) failed: {e}") from e
        return _to_order(o)

    def submit_trailing_stop(
        self,
        symbol: str,
        *,
        qty: float,
        trail_percent: float,
        client_order_id: str,
    ) -> Order:
        """Separate trailing-stop sell order (Alpaca's trailing stop is its own order type)."""
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        req = TrailingStopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=trail_percent * 100,   # Alpaca wants percent as 10.0, not 0.10
            client_order_id=client_order_id,
        )
        try:
            o = self._client.submit_order(req)
        except Exception as e:
            raise BrokerError(f"submit_trailing_stop({symbol}) failed: {e}") from e
        return _to_order(o)

    def get_filled_notional(self, client_order_id: str) -> float:
        """Return filled notional (filled_qty × filled_avg_price) for the order.
        Returns 0.0 if order unknown or unfilled. Never raises.
        """
        try:
            o = self._client.get_order_by_client_order_id(client_order_id)
        except Exception:
            return 0.0
        fq = getattr(o, "filled_qty", None)
        fap = getattr(o, "filled_avg_price", None)
        if fq is not None and fap is not None:
            try:
                return float(fq) * float(fap)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def cancel_order(self, order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(order_id)
        except Exception as e:
            raise BrokerError(f"cancel_order({order_id}) failed: {e}") from e

    def close_all_positions(self) -> None:
        """Test-setup helper. Closes every open position and cancels open orders.
        Safe to call on an empty account."""
        try:
            self._client.close_all_positions(cancel_orders=True)
        except Exception as e:
            raise BrokerError(f"close_all_positions failed: {e}") from e

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for symbol."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        md = StockHistoricalDataClient(api_key=key, secret_key=secret)
        try:
            resp = md.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )
        except Exception as e:
            raise BrokerError(f"latest_quote({symbol}) failed: {e}") from e
        q = resp[symbol]
        return float(q.bid_price), float(q.ask_price)

    def _latest_price(self, symbol: str) -> float:
        """Fetch the latest trade price via the market-data client."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
        md = StockHistoricalDataClient(api_key=key, secret_key=secret)
        try:
            resp = md.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
        except Exception as e:
            raise BrokerError(f"_latest_price({symbol}) failed: {e}") from e
        return float(resp[symbol].price)


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
        stop_price=float(o.stop_price) if getattr(o, "stop_price", None) is not None else None,
    )
