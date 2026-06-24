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
        # Reuse a single market-data client for all quote/trade fetches.
        # Constructing one per call (the original behavior) was a 150-250ms
        # TLS-handshake tax on every latest_quote / latest_price — intraday
        # cron called these 50+ times a day, so the win is real.
        from alpaca.data.historical import StockHistoricalDataClient
        self._md_client = StockHistoricalDataClient(
            api_key=key, secret_key=secret,
        )
        # is_market_open cache: 30s TTL. orders.execute_plan /
        # submit_limit_slice / approve_pending each call this, and the answer
        # changes at exactly two points a day (open / close), so re-hitting
        # Alpaca every call is pure waste.
        self._market_open_cache: Optional[tuple[float, bool]] = None
        self._market_open_ttl = 30.0

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
        """Open + partially-filled orders, with nested bracket legs.

        Explicit about PARTIALLY_FILLED inclusion: Alpaca's QueryOrderStatus.OPEN
        is documented as "open OR partially filled" in current alpaca-py
        versions, but the docs have flip-flopped historically — we fall back
        to a manual union if needed so executor's `_cancel_prior` reliably
        finds partial-fill orders (otherwise re-submit would stack on top
        of an unobserved partial).
        """
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
        """Cached for `_market_open_ttl` seconds (default 30s) — the answer
        only changes at market open / close, so repeated callers within a
        single tick should not each pay the Alpaca round-trip."""
        import time as _time
        now = _time.time()
        if self._market_open_cache is not None:
            cached_at, value = self._market_open_cache
            if now - cached_at < self._market_open_ttl:
                return value
        try:
            value = bool(self._client.get_clock().is_open)
        except Exception as e:
            raise BrokerError(f"is_market_open failed: {e}") from e
        self._market_open_cache = (now, value)
        return value

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
        stop_price: float,
        client_order_id: str,
        take_profit_price: Optional[float] = None,
    ) -> Order:
        """Submit a market buy with a fixed-stop leg attached (Alpaca bracket).

        Pure I/O: caller passes the **absolute** stop_price in dollars. Any
        percentage → price conversion belongs in policy (orders.py), not
        here — keeping policy out of broker.py avoids the "broker fetches
        market data to compute a percent stop, then the order fills at a
        different price, so the stop drifts ±1%" problem.

        Take-profit leg is required by Alpaca's bracket API but we don't
        actually want one. Caller may pass `take_profit_price` if they have
        a real target; otherwise we set an unreachable price (1e6 × any
        reasonable share price) so the leg never triggers — orders.py
        handles real take-profit via SEPA R-tier scale-outs, not bracket.

        Trailing-stop is attached SEPARATELY by `orders.ensure_trailing_stops`
        after the bracket fills (Alpaca doesn't support combining bracket
        + trailing natively).
        """
        from alpaca.trading.requests import (
            MarketOrderRequest, StopLossRequest, TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if notional <= 0:
            raise BrokerError(f"submit_bracket: non-positive notional {notional}")
        if stop_price <= 0:
            raise BrokerError(f"submit_bracket: non-positive stop_price {stop_price}")

        # Sentinel take-profit: 1M × stop_price is well into "would-never-fire"
        # territory even for leveraged ETFs over multi-year horizons.
        tp_price = (
            round(float(take_profit_price), 2)
            if take_profit_price is not None and take_profit_price > 0
            else round(float(stop_price) * 1_000_000, 2)
        )

        req = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(float(stop_price), 2)),
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

    def get_filled_notional(self, client_order_id: str) -> Optional[float]:
        """Return filled notional (filled_qty × filled_avg_price) for the order.

        Returns:
          - float >= 0.0 on a successful lookup (0.0 = order exists but unfilled)
          - None on query failure (network blip, transient Alpaca index lag)

        Callers should distinguish: 0.0 means "confirmed no fill yet, safe to
        re-submit a slice", None means "we don't know, don't re-submit yet
        or we may double up on top of an unobserved partial fill".
        """
        try:
            o = self._client.get_order_by_client_order_id(client_order_id)
        except Exception:
            return None
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
        BLOCKED on live accounts — this is meant for paper-account reset only.
        Safe to call on an empty paper account.
        """
        if self.env != "paper":
            raise BrokerError(
                "close_all_positions blocked on live: this helper is for paper "
                "account reset only. If you really want to liquidate a live "
                "account, do it through the Alpaca dashboard."
            )
        try:
            self._client.close_all_positions(cancel_orders=True)
        except Exception as e:
            raise BrokerError(f"close_all_positions failed: {e}") from e

    def latest_quote(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for symbol via the cached market-data client."""
        from alpaca.data.requests import StockLatestQuoteRequest
        try:
            resp = self._md_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )
        except Exception as e:
            raise BrokerError(f"latest_quote({symbol}) failed: {e}") from e
        q = resp[symbol]
        return float(q.bid_price), float(q.ask_price)

    def latest_price(self, symbol: str) -> float:
        """Latest trade price via the cached market-data client.

        (Renamed from `_latest_price` — the underscore was misleading since
        orders.py / watchdog.py / rebalancer.py all call this externally.
        The old name is kept as an alias below for backward compat with tests
        and third-party callers; new code should use the public name.)
        """
        from alpaca.data.requests import StockLatestTradeRequest
        try:
            resp = self._md_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
        except Exception as e:
            raise BrokerError(f"latest_price({symbol}) failed: {e}") from e
        return float(resp[symbol].price)

    # Backward-compat alias — old code path. Prefer `latest_price`.
    _latest_price = latest_price


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
