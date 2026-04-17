# Alpaca Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Alpaca as the trading broker for the quant system, enabling fully-automated order submission (paper-first) behind a four-layer safety policy (HALT file, paper/live guard, daily caps, Telegram approval for orders ≥ $2K).

**Architecture:** Layered modules `broker.py` (Alpaca SDK wrapper, pure I/O) → `orders.py` (policy layer: diffing, safety, pending queue) → `rebalancer.py` (new entry point) and refactored `watchdog.py` (signal-driven exits). Alpaca is source of truth; `portfolio.json` becomes a cache + tranche metadata. See `docs/superpowers/specs/2026-04-17-alpaca-integration-design.md` for full design.

**Tech Stack:** `alpaca-py` (official SDK), `pytest` + `pytest-mock` for tests, existing signal modules (`momentum.py`, `screener.py`, `macro.py`) unchanged.

---

## File Structure

**New files**
- `broker.py` — Alpaca SDK wrapper; one class `Broker` + plain-data result types.
- `orders.py` — Policy layer: `sync_state`, `reconcile_to_targets`, `execute_plan`, `submit_exit`, pending-queue helpers. Holds `OrderIntent`, `OrderPlan`, `PortfolioSnapshot`, `ExecutionResult` dataclasses.
- `rebalancer.py` — Cron entry point; builds target weights per tranche and runs them through `orders.py`.
- `tests/__init__.py` — empty, so pytest discovers `tests/`.
- `tests/fakes.py` — `FakeBroker` and `FakeClock` test doubles.
- `tests/test_broker.py` — unit tests for the live-confirm guard + construction.
- `tests/test_orders.py` — heavy unit tests covering every safety rail path.
- `tests/test_rebalancer.py` — integration between `rebalancer.py` and `orders.py` with `FakeBroker`.
- `tests/test_integration.py` — opt-in integration tests against live Alpaca paper.
- `.env.example` — documents the env var names the system reads.

**Modified files**
- `config.py` — add Alpaca settings, safety caps, rebalance cadence dict.
- `watchdog.py` — read state via `orders.sync_state`, route exits through `orders.submit_exit`, verify brackets exist.
- `run.py` — strip portfolio-construction; becomes a read-only reporter.
- `requirements.txt` — add `alpaca-py`, `pytest`, `pytest-mock`, `python-dotenv`.
- `.gitignore` — add `pending_orders.json`, `.cache/daily_trade_log.json`, `.cache/HALT`, `.pytest_cache/`.
- `README.md` — replace "place orders through your broker" with Alpaca workflow.

**Deleted/no-longer-authoritative**
- `watchdog.init_portfolio()` — obsolete; `portfolio.json` is now built by `sync_state`.

---

## Conventions

- All new Python uses type hints. Dataclasses are `frozen=True` where they model immutable records (intents, plans, snapshots) and mutable where they represent state that's built up (e.g., `ExecutionResult`).
- Every commit passes `python3 -m pytest tests/ -q` (unit tests only, no `-m integration`).
- Use `datetime.datetime.now(tz=datetime.UTC)` throughout. No naive datetimes.
- Every order carries a `client_order_id` built by `orders._make_cid(tranche, reason, symbol, today)` → `"{tranche}-{reason}-{symbol}-{YYYYMMDD}-{6-char-hash}"`.
- Tests use `FakeBroker` and `FakeClock`; never touch the network.

---

### Task 1: Project scaffold

**Files:**
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Create: `.env.example`
- Create: `tests/__init__.py`
- Create: `pytest.ini`

- [ ] **Step 1: Add dependencies to `requirements.txt`**

Replace the contents of `requirements.txt` with:

```
yfinance>=0.2.0
pandas>=1.3.0
numpy>=1.21.0
scipy>=1.7.0
tabulate>=0.9.0
fredapi>=0.5.0
alpaca-py>=0.21.0
python-dotenv>=1.0.0
pytest>=7.4.0
pytest-mock>=3.12.0
```

- [ ] **Step 2: Install**

Run:
```bash
cd /Users/zl/works/stock && pip3 install -r requirements.txt
```
Expected: all installs succeed.

- [ ] **Step 3: Update `.gitignore`**

Replace the contents of `.gitignore` with:

```
.cache/
__pycache__/
*.pyc
.env
.worktrees/
.pytest_cache/
pending_orders.json
portfolio.json
```

(`portfolio.json` is now a cache rebuildable from Alpaca — gitignored. `pending_orders.json` contains live order intents — must be gitignored.)

- [ ] **Step 4: Create `.env.example`**

Create `/Users/zl/works/stock/.env.example`:

```
# ── FRED (existing) ──
FRED_API_KEY=your_fred_key_here

# ── Alpaca ──
# Get keys at https://app.alpaca.markets/paper/dashboard/overview (paper)
# or https://app.alpaca.markets/brokerage/dashboard/overview (live)
ALPACA_API_KEY=
ALPACA_API_SECRET=

# Environment selector: "paper" (default) or "live"
ALPACA_ENV=paper

# Required ONLY when ALPACA_ENV=live. Must be literally "yes" to arm live trading.
ALPACA_LIVE_CONFIRM=
```

- [ ] **Step 5: Create `tests/__init__.py`**

Create `/Users/zl/works/stock/tests/__init__.py` as an empty file.

- [ ] **Step 6: Create `pytest.ini`**

Create `/Users/zl/works/stock/pytest.ini`:

```ini
[pytest]
testpaths = tests
markers =
    integration: tests that call the real Alpaca API (opt-in)
filterwarnings =
    ignore::DeprecationWarning
addopts = -q --strict-markers -m "not integration"
```

The `-m "not integration"` default ensures plain `pytest` skips the network tests. Run them with `pytest -m integration`.

- [ ] **Step 7: Verify pytest runs cleanly with no tests**

Run:
```bash
cd /Users/zl/works/stock && python3 -m pytest tests/ -q
```
Expected: `no tests ran`.

- [ ] **Step 8: Commit**

```bash
cd /Users/zl/works/stock
git add requirements.txt .gitignore .env.example tests/__init__.py pytest.ini
git commit -m "chore: scaffold alpaca integration (deps, pytest, env example)"
```

---

### Task 2: Config additions

**Files:**
- Modify: `config.py` — append Alpaca section

- [ ] **Step 1: Append Alpaca + safety settings to `config.py`**

Append to the end of `/Users/zl/works/stock/config.py`:

```python
# ── Alpaca broker ───────────────────────────────────────────────
ALPACA_ENV = os.environ.get("ALPACA_ENV", "paper")         # "paper" | "live"
ALPACA_LIVE_CONFIRM = os.environ.get("ALPACA_LIVE_CONFIRM") == "yes"
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")

# Alpaca API endpoints. The SDK picks these automatically from env, but
# surfaced here for clarity / dry-run / test overrides.
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"

# ── Safety rails ────────────────────────────────────────────────
HALT_PATH = os.path.join(os.path.dirname(__file__), ".cache", "HALT")
DAILY_TRADE_LOG = os.path.join(os.path.dirname(__file__), ".cache", "daily_trade_log.json")
PENDING_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "pending_orders.json")

DAILY_MAX_ORDERS = 20
DAILY_MAX_NOTIONAL = 25_000
LARGE_ORDER_THRESHOLD = 2_000
PENDING_ORDER_TTL_HOURS = 6

# ── Rebalance cadence per tranche ───────────────────────────────
# Core cadence comes from the active mode; aggressive is fixed (weekly).
REBALANCE_DAYS = {
    "core": _params["rebalance_days"],
    "aggressive": AGGRESSIVE_PARAMS["rebalance_days"],
}
```

- [ ] **Step 2: Sanity-check import**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "from config import ALPACA_ENV, REBALANCE_DAYS, HALT_PATH, LARGE_ORDER_THRESHOLD; print(ALPACA_ENV, REBALANCE_DAYS)"
```
Expected: `paper {'core': 30, 'aggressive': 7}` (or whatever the active mode gives for core).

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/stock
git add config.py
git commit -m "feat: add Alpaca + safety-rail config"
```

---

### Task 3: Broker data types

**Files:**
- Create: `broker.py` (first pass — types only)

- [ ] **Step 1: Create `broker.py` with dataclasses only**

Create `/Users/zl/works/stock/broker.py`:

```python
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
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "from broker import AccountSnapshot, Position, Order, BrokerError, ConfigError; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/stock
git add broker.py
git commit -m "feat(broker): add result-type dataclasses"
```

---

### Task 4: Broker class — construction + live-confirm guard

**Files:**
- Modify: `broker.py`
- Create: `tests/test_broker.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/zl/works/stock/tests/test_broker.py`:

```python
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
```

- [ ] **Step 2: Run the test — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_broker.py -v
```
Expected: ImportError or AttributeError on `Broker` — class doesn't exist yet.

- [ ] **Step 3: Implement the `Broker` constructor**

Append to `/Users/zl/works/stock/broker.py`:

```python
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
```

- [ ] **Step 4: Run the tests — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_broker.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add broker.py tests/test_broker.py
git commit -m "feat(broker): add Broker class with live-confirm guard"
```

---

### Task 5: Broker methods — account, positions, orders, market status

**Files:**
- Modify: `broker.py`

These methods are thin mappings from alpaca-py objects to our dataclasses. Unit-testing them meaningfully requires mocking the SDK; we defer the coverage to `FakeBroker` in the next task and integration tests at the end. Write straightforward code with no branching beyond None-handling.

- [ ] **Step 1: Add `get_account`, `get_positions`, `get_open_orders`, `is_market_open`**

Append to `/Users/zl/works/stock/broker.py`:

```python
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
```

- [ ] **Step 2: Verify the module still imports cleanly**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "from broker import Broker, _to_order; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/stock
git add broker.py
git commit -m "feat(broker): add read methods (account, positions, orders, clock)"
```

---

### Task 6: Broker methods — order submission + close-all

**Files:**
- Modify: `broker.py`

- [ ] **Step 1: Add `submit_market`, `submit_bracket`, `cancel_order`, `close_all_positions`**

Append to `/Users/zl/works/stock/broker.py`:

```python
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
```

- [ ] **Step 2: Smoke-check imports**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "from broker import Broker; print(hasattr(Broker, 'submit_bracket'))"
```
Expected: `True`.

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/stock
git add broker.py
git commit -m "feat(broker): add order submission + close_all helpers"
```

---

### Task 7: FakeBroker test double

**Files:**
- Create: `tests/fakes.py`

- [ ] **Step 1: Create `tests/fakes.py`**

Create `/Users/zl/works/stock/tests/fakes.py`:

```python
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
        default_factory=lambda: dt.datetime(2026, 4, 17, 14, 0, 0, tzinfo=dt.UTC)
    )

    def now(self) -> dt.datetime:
        return self.now_value

    def advance(self, seconds: float):
        self.now_value = self.now_value + dt.timedelta(seconds=seconds)
```

- [ ] **Step 2: Verify import**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "from tests.fakes import FakeBroker, FakeClock; b = FakeBroker(); print(b.get_account())"
```
Expected: `AccountSnapshot(cash=100000.0, equity=100000.0, buying_power=100000.0)`.

- [ ] **Step 3: Commit**

```bash
cd /Users/zl/works/stock
git add tests/fakes.py
git commit -m "test: add FakeBroker and FakeClock doubles"
```

---

### Task 8: Orders dataclasses + client_order_id helper

**Files:**
- Create: `orders.py` (first pass — types + cid builder + simple helpers)
- Create: `tests/test_orders.py` (first pass — cid + types)

- [ ] **Step 1: Write the failing test for client_order_id format**

Create `/Users/zl/works/stock/tests/test_orders.py`:

```python
"""Unit tests for orders.py — heaviest coverage in the codebase (safety rails)."""
import datetime as dt
import json
import os
import re
import pytest

from broker import BrokerError
from tests.fakes import FakeBroker, FakeClock


def test_make_cid_format():
    from orders import _make_cid
    cid = _make_cid(tranche="core", reason="rebalance", symbol="SPY",
                    today=dt.date(2026, 4, 17))
    assert re.fullmatch(r"core-rebalance-SPY-20260417-[0-9a-f]{6}", cid), cid


def test_make_cid_deterministic_per_day():
    from orders import _make_cid
    a = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    b = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    assert a == b


def test_make_cid_varies_by_day():
    from orders import _make_cid
    a = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 17))
    b = _make_cid("core", "rebalance", "SPY", dt.date(2026, 4, 18))
    assert a != b
```

- [ ] **Step 2: Run the tests — expect ImportError**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: import errors or AttributeError on `_make_cid`.

- [ ] **Step 3: Create `orders.py` with dataclasses + cid helper**

Create `/Users/zl/works/stock/orders.py`:

```python
"""
Orders / policy layer. All Alpaca-touching decisions funnel through here:
  - state reconciliation (sync_state)
  - target-to-order diffing (reconcile_to_targets)
  - safety rails (HALT, daily caps, large-order gate)
  - pending-order queue (for Telegram approval)

Callers: rebalancer.py, watchdog.py, telegram bot.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

import config
from broker import (
    Broker, BrokerError, AccountSnapshot, Position, Order,
)

# ── Types ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    notional: float
    side: str                       # "buy" | "sell"
    reason: str
    tranche: str
    client_order_id: str
    stop_pct: Optional[float] = None     # set on entries
    trail_pct: Optional[float] = None    # set on entries


@dataclass(frozen=True)
class OrderPlan:
    buys: list[OrderIntent]
    sells: list[OrderIntent]
    holds: list[str]


@dataclass
class ExecutionResult:
    submitted: list[Order] = field(default_factory=list)
    queued: list[OrderIntent] = field(default_factory=list)
    skipped: list[tuple[OrderIntent, str]] = field(default_factory=list)
    deferred: list[OrderIntent] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioSnapshot:
    synced_at: str
    alpaca_env: str
    cash: float
    equity: float
    positions: list[dict]           # enriched: base position + tranche/entry_reason/stop_ids
    tranches: dict                  # {"core": {"last_rebalance": "YYYY-MM-DD"}, ...}

    def by_tranche(self, tranche: str) -> list[dict]:
        return [p for p in self.positions if p.get("tranche") == tranche]


# ── Client order ID ─────────────────────────────────────────────

def _make_cid(tranche: str, reason: str, symbol: str, today: dt.date) -> str:
    """Deterministic per (tranche, reason, symbol, day). Alpaca rejects duplicates,
    giving us free idempotency across cron re-runs within a day."""
    key = f"{tranche}|{reason}|{symbol}|{today.isoformat()}"
    h = hashlib.sha1(key.encode()).hexdigest()[:6]
    return f"{tranche}-{reason}-{symbol}-{today.strftime('%Y%m%d')}-{h}"
```

- [ ] **Step 4: Run the tests — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): types + deterministic client_order_id helper"
```

---

### Task 9: sync_state — basic snapshot + metadata merge

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── sync_state ──────────────────────────────────────────────────

def _portfolio_cache(tmp_path, monkeypatch, data):
    """Point PORTFOLIO_PATH/etc at tmp dir and seed portfolio.json."""
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def test_sync_state_carries_forward_known_tranche(tmp_path, monkeypatch):
    from orders import sync_state

    old = {
        "synced_at": "2026-04-16T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance 2026-04-16",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-04-16"},
                     "aggressive": {"last_rebalance": "2026-04-16"}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500, mv=5050)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["tranche"] == "core"
    assert p["entry_reason"] == "core rebalance 2026-04-16"
    assert p["market_value"] == 5050


def test_sync_state_marks_unknown_tranche(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, None)  # no cache

    fb = FakeBroker()
    fb.seed_position("NVDA", qty=5, avg_entry=100, mv=520)

    alerts: list = []
    snap = sync_state(fb, alerts=alerts)

    assert snap.positions[0]["tranche"] == "unknown"
    assert any("unknown" in a.lower() and "NVDA" in a for a in alerts)


def test_sync_state_drops_closed_positions(tmp_path, monkeypatch):
    from orders import sync_state

    old = {
        "synced_at": "2026-04-16T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "SPY", "shares": 10.0, "avg_entry": 500.0,
             "market_value": 5000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-04-16"},
                     "aggressive": {"last_rebalance": "2026-04-16"}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    fb = FakeBroker()  # no positions seeded — SPY was closed

    snap = sync_state(fb, alerts=[])
    assert snap.positions == []


def test_sync_state_flags_missing_bracket(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, None)
    fb = FakeBroker()
    fb.seed_position("SPY", qty=10, avg_entry=500)
    # no open orders seeded => bracket is missing

    alerts: list = []
    sync_state(fb, alerts=alerts)
    assert any("bracket" in a.lower() and "SPY" in a for a in alerts)
```

- [ ] **Step 2: Run — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: new tests fail (missing `sync_state`, `PORTFOLIO_PATH`, `DAILY_LOG_PATH`).

- [ ] **Step 3: Implement `sync_state`**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── Paths (overridable for tests) ───────────────────────────────

PORTFOLIO_PATH = os.path.join(os.path.dirname(__file__), "portfolio.json")
DAILY_LOG_PATH = os.path.join(os.path.dirname(__file__), "daily_log.csv")


# ── sync_state ──────────────────────────────────────────────────

def _load_portfolio_cache() -> dict:
    if not os.path.exists(PORTFOLIO_PATH):
        return {"positions": [], "tranches": {
            "core": {"last_rebalance": None},
            "aggressive": {"last_rebalance": None},
        }}
    with open(PORTFOLIO_PATH) as f:
        return json.load(f)


def _save_portfolio_cache(snap: PortfolioSnapshot):
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(asdict(snap), f, indent=2, default=str)


def _append_daily_log(line: str):
    os.makedirs(os.path.dirname(DAILY_LOG_PATH), exist_ok=True) if os.path.dirname(DAILY_LOG_PATH) else None
    with open(DAILY_LOG_PATH, "a") as f:
        f.write(line + "\n")


def sync_state(broker, *, alerts: Optional[list] = None) -> PortfolioSnapshot:
    """Fetch live positions from Alpaca, merge local metadata, write cache.

    `alerts` (if provided) receives human-readable strings for anomalies:
      - positions on Alpaca we don't have metadata for → tranche 'unknown'
      - positions missing their bracket/trailing-stop orders
    """
    if alerts is None:
        alerts = []

    acc = broker.get_account()
    live = broker.get_positions()
    open_orders = broker.get_open_orders()
    cache = _load_portfolio_cache()

    # Index local metadata by symbol
    old_meta = {p["symbol"]: p for p in cache.get("positions", [])}

    # Index open orders by (symbol, type) for bracket verification
    stops_by_symbol: dict[str, str] = {}
    trails_by_symbol: dict[str, str] = {}
    for o in open_orders:
        if o.type in ("stop", "stop_loss"):
            stops_by_symbol[o.symbol] = o.id
        elif o.type == "trailing_stop":
            trails_by_symbol[o.symbol] = o.id

    positions: list[dict] = []
    live_symbols = {p.symbol for p in live}

    for p in live:
        meta = old_meta.get(p.symbol)
        if meta is None:
            alerts.append(f"Unknown position on Alpaca: {p.symbol} ({p.qty} sh). "
                          f"Tag with orders.tag_position('{p.symbol}', 'core'|'aggressive').")
            tranche = "unknown"
            entry_reason = "external"
        else:
            tranche = meta.get("tranche", "unknown")
            entry_reason = meta.get("entry_reason", "unknown")

        stop_id = stops_by_symbol.get(p.symbol)
        trail_id = trails_by_symbol.get(p.symbol)
        if tranche != "unknown" and stop_id is None and trail_id is None:
            alerts.append(f"No bracket/trailing stop attached to {p.symbol} — "
                          "stop protection inactive.")

        positions.append({
            "symbol": p.symbol,
            "shares": p.qty,
            "avg_entry": p.avg_entry,
            "market_value": p.market_value,
            "unrealized_pl": p.unrealized_pl,
            "tranche": tranche,
            "entry_reason": entry_reason,
            "stop_order_id": stop_id,
            "trail_order_id": trail_id,
        })

    # Emit "closed" events for cached positions that vanished
    for sym, meta in old_meta.items():
        if sym not in live_symbols:
            _append_daily_log(f"{dt.datetime.now(dt.UTC).isoformat()},CLOSED,{sym},"
                              f"{meta.get('tranche','unknown')},{meta.get('entry_reason','')}")

    tranches = cache.get("tranches", {
        "core": {"last_rebalance": None},
        "aggressive": {"last_rebalance": None},
    })

    snap = PortfolioSnapshot(
        synced_at=dt.datetime.now(dt.UTC).isoformat(),
        alpaca_env=getattr(broker, "env", "paper"),
        cash=acc.cash,
        equity=acc.equity,
        positions=positions,
        tranches=tranches,
    )
    _save_portfolio_cache(snap)

    # Append an equity snapshot
    _append_daily_log(f"{snap.synced_at},EQUITY,{snap.equity:.2f},{snap.cash:.2f}")
    return snap
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): sync_state with metadata merge and bracket verification"
```

---

### Task 10: reconcile_to_targets

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── reconcile_to_targets ────────────────────────────────────────

def _snap(positions, cash=10_000, equity=100_000):
    from orders import PortfolioSnapshot
    return PortfolioSnapshot(
        synced_at="2026-04-17T14:00:00+00:00",
        alpaca_env="paper",
        cash=cash, equity=equity,
        positions=positions,
        tranches={"core": {"last_rebalance": None},
                  "aggressive": {"last_rebalance": None}},
    )


def test_reconcile_opens_new_positions(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    snap = _snap(positions=[], cash=90_000, equity=90_000)
    plan = reconcile_to_targets(
        {"SPY": 0.5, "QQQ": 0.5},
        tranche="core",
        snapshot=snap,
        tranche_capital=90_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 2
    buy_syms = sorted(i.symbol for i in plan.buys)
    assert buy_syms == ["QQQ", "SPY"]
    assert all(i.notional == 45_000 for i in plan.buys)
    assert all(i.tranche == "core" for i in plan.buys)
    assert all(i.stop_pct is not None and i.trail_pct is not None for i in plan.buys)


def test_reconcile_closes_removed_positions(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "TSLA", "shares": 10, "avg_entry": 300,
         "market_value": 3000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    sell_syms = [i.symbol for i in plan.sells]
    assert sell_syms == ["TSLA"]
    assert plan.sells[0].notional == 3000


def test_reconcile_ignores_unknown_tranche(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "NVDA", "shares": 5, "avg_entry": 100,
         "market_value": 520, "unrealized_pl": 0,
         "tranche": "unknown", "entry_reason": "external",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    # NVDA is unknown — should not be sold
    assert all(i.symbol != "NVDA" for i in plan.sells)


def test_reconcile_rebalance_within_tranche(tmp_path, monkeypatch):
    from orders import reconcile_to_targets

    positions = [
        {"symbol": "SPY", "shares": 10, "avg_entry": 500,
         "market_value": 6000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
        {"symbol": "QQQ", "shares": 5, "avg_entry": 400,
         "market_value": 2000, "unrealized_pl": 0,
         "tranche": "core", "entry_reason": "x",
         "stop_order_id": None, "trail_order_id": None},
    ]
    snap = _snap(positions=positions)
    plan = reconcile_to_targets(
        {"SPY": 0.4, "QQQ": 0.4, "IWM": 0.2},
        tranche="core", snapshot=snap, tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    # Targets: SPY $4000 (down from $6000), QQQ $4000 (up from $2000), IWM $2000 new
    got = {i.symbol: (i.side, i.notional) for i in plan.buys + plan.sells}
    assert got["SPY"] == ("sell", 2000)
    assert got["QQQ"] == ("buy", 2000)
    assert got["IWM"] == ("buy", 2000)
```

- [ ] **Step 2: Run — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 4 new failures.

- [ ] **Step 3: Implement `reconcile_to_targets`**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── Stop / trailing-stop percentages per tranche ────────────────

def _tranche_stops(tranche: str) -> tuple[float, float]:
    if tranche == "aggressive":
        ap = config.AGGRESSIVE_PARAMS
        return ap["stop_loss_pct"], ap["trailing_stop_pct"]
    return config.STOP_LOSS_PCT, config.TRAILING_STOP_PCT


# ── reconcile_to_targets ────────────────────────────────────────

_REBALANCE_BAND_PCT = 0.05   # ignore drifts smaller than 5% of tranche capital


def reconcile_to_targets(
    targets: dict[str, float],
    *,
    tranche: str,
    snapshot: PortfolioSnapshot,
    tranche_capital: float,
    today: dt.date,
) -> OrderPlan:
    """Diff target weights against current positions for the given tranche.

    targets: {symbol: fraction_of_tranche_capital}. Fractions summing to <1 leave the
    remainder in cash. Unknown-tranche positions are ignored (neither sold nor counted).
    Drifts smaller than `_REBALANCE_BAND_PCT * tranche_capital` are treated as holds.
    """
    stop_pct, trail_pct = _tranche_stops(tranche)
    held = {p["symbol"]: p for p in snapshot.by_tranche(tranche)}

    target_dollars = {sym: frac * tranche_capital for sym, frac in targets.items()}
    band = tranche_capital * _REBALANCE_BAND_PCT

    buys: list[OrderIntent] = []
    sells: list[OrderIntent] = []
    holds: list[str] = []

    all_symbols = set(target_dollars) | set(held)
    for sym in sorted(all_symbols):
        current_mv = held.get(sym, {}).get("market_value", 0.0)
        target_mv = target_dollars.get(sym, 0.0)
        diff = target_mv - current_mv

        if abs(diff) < band and sym in held:
            holds.append(sym)
            continue

        reason = f"{tranche} rebalance"
        if diff > 0:
            cid = _make_cid(tranche, "rebalance", sym, today)
            buys.append(OrderIntent(
                symbol=sym, notional=round(diff, 2), side="buy",
                reason=reason, tranche=tranche, client_order_id=cid,
                stop_pct=stop_pct, trail_pct=trail_pct,
            ))
        elif diff < 0:
            # Selling: notional is the amount to reduce by.
            cid = _make_cid(tranche, "rebalance-sell", sym, today)
            sells.append(OrderIntent(
                symbol=sym, notional=round(abs(diff), 2), side="sell",
                reason=reason, tranche=tranche, client_order_id=cid,
            ))

    return OrderPlan(buys=buys, sells=sells, holds=holds)
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): reconcile_to_targets with per-tranche scoping and drift band"
```

---

### Task 11: Safety rail — HALT file

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing test**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── HALT ────────────────────────────────────────────────────────

def _safety_paths(tmp_path, monkeypatch):
    """Redirect all safety-rail paths into tmp_path."""
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))


def test_halt_blocks_all_orders(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    (tmp_path / "HALT").write_text("paused")

    from orders import OrderIntent, OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[OrderIntent(symbol="SPY", notional=500, side="buy",
                           reason="test", tranche="core",
                           client_order_id="cid-1",
                           stop_pct=0.08, trail_pct=0.12)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert result.submitted == []
    assert len(result.skipped) == 1
    assert "HALT" in result.skipped[0][1]
```

- [ ] **Step 2: Run — expect failure**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: new test fails (no `execute_plan`).

- [ ] **Step 3: Implement HALT check + `execute_plan` skeleton**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── Safety-rail paths (overridable for tests) ───────────────────

HALT_PATH = config.HALT_PATH
DAILY_TRADE_LOG = config.DAILY_TRADE_LOG
PENDING_ORDERS_PATH = config.PENDING_ORDERS_PATH


# ── execute_plan (scaffolded with HALT only; caps/large-order added in later tasks)

def execute_plan(plan: OrderPlan, *, broker, reason: str) -> ExecutionResult:
    """Runs every intent through: HALT → market-open → daily caps → large-order gate."""
    result = ExecutionResult()
    intents = list(plan.sells) + list(plan.buys)   # sells first: free up buying power

    if os.path.exists(HALT_PATH):
        for i in intents:
            result.skipped.append((i, "HALT file present"))
        return result

    for i in intents:
        # Caps + large-order gate added in later tasks
        _submit_intent(broker, i, result)

    return result


def _submit_intent(broker, i: OrderIntent, result: ExecutionResult):
    """Submit a single intent via the appropriate broker method. Catches BrokerError."""
    try:
        if i.side == "buy":
            if i.stop_pct is not None and i.trail_pct is not None:
                o = broker.submit_bracket(
                    i.symbol, notional=i.notional,
                    stop_loss_pct=i.stop_pct, trailing_stop_pct=i.trail_pct,
                    client_order_id=i.client_order_id,
                )
            else:
                o = broker.submit_market(
                    i.symbol, notional=i.notional, side="buy",
                    client_order_id=i.client_order_id,
                )
        else:
            o = broker.submit_market(
                i.symbol, notional=i.notional, side="sell",
                client_order_id=i.client_order_id,
            )
        result.submitted.append(o)
    except BrokerError as e:
        result.skipped.append((i, f"BrokerError: {e}"))
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): HALT safety rail + execute_plan scaffold"
```

---

### Task 12: Safety rail — daily caps

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── Daily caps ──────────────────────────────────────────────────

def _intent(sym, notional, side="buy"):
    from orders import OrderIntent
    return OrderIntent(
        symbol=sym, notional=notional, side=side,
        reason="test", tranche="core",
        client_order_id=f"core-test-{sym}-20260417-abcdef",
        stop_pct=0.08 if side == "buy" else None,
        trail_pct=0.12 if side == "buy" else None,
    )


def test_daily_max_orders_cap(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 2)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("A", 100), _intent("B", 100), _intent("C", 100)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert len(result.submitted) == 2
    assert len(result.deferred) == 1


def test_daily_max_notional_cap(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 500)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("A", 300), _intent("B", 300)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert len(result.submitted) == 1
    assert len(result.deferred) == 1


def test_caps_persist_across_calls(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 2)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import OrderPlan, execute_plan
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("A", 100)], sells=[], holds=[]),
                 broker=fb, reason="t1")
    execute_plan(OrderPlan(buys=[_intent("B", 100)], sells=[], holds=[]),
                 broker=fb, reason="t2")
    # Third call should defer — already at 2 submitted today
    r3 = execute_plan(OrderPlan(buys=[_intent("C", 100)], sells=[], holds=[]),
                       broker=fb, reason="t3")
    assert r3.submitted == []
    assert len(r3.deferred) == 1
```

- [ ] **Step 2: Run — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 3 new failures.

- [ ] **Step 3: Implement daily cap reading + enforcement**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── Daily caps ──────────────────────────────────────────────────

DAILY_MAX_ORDERS = config.DAILY_MAX_ORDERS
DAILY_MAX_NOTIONAL = config.DAILY_MAX_NOTIONAL


def _today_key(now: Optional[dt.datetime] = None) -> str:
    return (now or dt.datetime.now(dt.UTC)).date().isoformat()


def _load_daily_log() -> dict:
    if not os.path.exists(DAILY_TRADE_LOG):
        return {}
    with open(DAILY_TRADE_LOG) as f:
        return json.load(f)


def _save_daily_log(log: dict):
    os.makedirs(os.path.dirname(DAILY_TRADE_LOG), exist_ok=True) if os.path.dirname(DAILY_TRADE_LOG) else None
    with open(DAILY_TRADE_LOG, "w") as f:
        json.dump(log, f, indent=2)


def _today_bucket(log: dict) -> dict:
    key = _today_key()
    if key not in log:
        log[key] = {"submitted_count": 0, "submitted_notional": 0.0, "deferred": []}
    return log[key]
```

Now replace the existing `execute_plan` body with cap enforcement. **Delete** the current body of `execute_plan` (keeping its signature) and paste:

```python
def execute_plan(plan: OrderPlan, *, broker, reason: str) -> ExecutionResult:
    """Runs every intent through: HALT → market-open → daily caps → large-order gate."""
    result = ExecutionResult()
    intents = list(plan.sells) + list(plan.buys)

    if os.path.exists(HALT_PATH):
        for i in intents:
            result.skipped.append((i, "HALT file present"))
        return result

    log = _load_daily_log()
    bucket = _today_bucket(log)

    for i in intents:
        # Daily cap enforcement
        if bucket["submitted_count"] >= DAILY_MAX_ORDERS:
            result.deferred.append(i)
            bucket["deferred"].append(asdict(i))
            continue
        if bucket["submitted_notional"] + i.notional > DAILY_MAX_NOTIONAL:
            result.deferred.append(i)
            bucket["deferred"].append(asdict(i))
            continue

        before = len(result.submitted)
        _submit_intent(broker, i, result)
        if len(result.submitted) > before:
            bucket["submitted_count"] += 1
            bucket["submitted_notional"] += i.notional

    _save_daily_log(log)
    return result
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): daily-cap safety rail (count + notional)"
```

---

### Task 13: Safety rail — large-order pending queue

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── Large-order gate ────────────────────────────────────────────

def test_large_order_queued_not_submitted(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan
    plan = OrderPlan(
        buys=[_intent("SMALL", 500), _intent("BIG", 2_500)],
        sells=[], holds=[],
    )
    fb = FakeBroker()
    result = execute_plan(plan, broker=fb, reason="test")
    assert [o.symbol for o in result.submitted] == ["SMALL"]
    assert [i.symbol for i in result.queued] == ["BIG"]

    pending = json.loads((tmp_path / "pending_orders.json").read_text())
    assert len(pending) == 1 and pending[0]["symbol"] == "BIG"
    assert "expires" in pending[0]


def test_approve_pending_submits(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.DAILY_MAX_ORDERS", 100)
    monkeypatch.setattr("orders.DAILY_MAX_NOTIONAL", 100_000)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    assert len(pending) == 1
    result = approve_pending(pending[0]["id"], broker=fb)
    assert len(result.submitted) == 1
    assert list_pending() == []


def test_reject_pending_removes(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)

    from orders import OrderPlan, execute_plan, reject_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    reject_pending(pending[0]["id"])
    assert list_pending() == []


def test_approve_expired_rejected(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, None)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 1_000)
    monkeypatch.setattr("orders.PENDING_ORDER_TTL_HOURS", 0)  # expires instantly

    from orders import OrderPlan, execute_plan, approve_pending, list_pending
    fb = FakeBroker()
    execute_plan(OrderPlan(buys=[_intent("BIG", 2_500)], sells=[], holds=[]),
                 broker=fb, reason="test")
    pending = list_pending()
    result = approve_pending(pending[0]["id"], broker=fb)
    assert result.submitted == []
    assert any("expired" in msg.lower() for _, msg in result.skipped)
    assert list_pending() == []
```

- [ ] **Step 2: Run — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```

- [ ] **Step 3: Implement pending-queue plumbing**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── Large-order pending queue ───────────────────────────────────

LARGE_ORDER_THRESHOLD = config.LARGE_ORDER_THRESHOLD
PENDING_ORDER_TTL_HOURS = config.PENDING_ORDER_TTL_HOURS


def _load_pending() -> list[dict]:
    if not os.path.exists(PENDING_ORDERS_PATH):
        return []
    with open(PENDING_ORDERS_PATH) as f:
        return json.load(f)


def _save_pending(items: list[dict]):
    with open(PENDING_ORDERS_PATH, "w") as f:
        json.dump(items, f, indent=2)


def _intent_to_pending(i: OrderIntent, now: dt.datetime) -> dict:
    return {
        "id": f"pend_{i.client_order_id}",
        "symbol": i.symbol,
        "notional": i.notional,
        "side": i.side,
        "stop_pct": i.stop_pct,
        "trail_pct": i.trail_pct,
        "reason": i.reason,
        "tranche": i.tranche,
        "client_order_id": i.client_order_id,
        "created": now.isoformat(),
        "expires": (now + dt.timedelta(hours=PENDING_ORDER_TTL_HOURS)).isoformat(),
    }


def _pending_to_intent(p: dict) -> OrderIntent:
    return OrderIntent(
        symbol=p["symbol"], notional=p["notional"], side=p["side"],
        reason=p["reason"], tranche=p["tranche"],
        client_order_id=p["client_order_id"],
        stop_pct=p.get("stop_pct"), trail_pct=p.get("trail_pct"),
    )


def list_pending() -> list[dict]:
    return _load_pending()


def reject_pending(pending_id: str) -> None:
    items = _load_pending()
    _save_pending([p for p in items if p["id"] != pending_id])


def approve_pending(pending_id: str, *, broker) -> ExecutionResult:
    """Re-runs HALT + daily cap checks before submitting."""
    result = ExecutionResult()
    items = _load_pending()
    target = next((p for p in items if p["id"] == pending_id), None)
    if target is None:
        result.skipped.append((None, f"pending id not found: {pending_id}"))  # type: ignore[arg-type]
        return result

    # Remove from queue immediately — win or lose, it leaves the queue.
    _save_pending([p for p in items if p["id"] != pending_id])

    now = dt.datetime.now(dt.UTC)
    expires = dt.datetime.fromisoformat(target["expires"])
    intent = _pending_to_intent(target)

    if now > expires:
        result.skipped.append((intent, "pending order expired"))
        return result

    if os.path.exists(HALT_PATH):
        result.skipped.append((intent, "HALT file present"))
        return result

    log = _load_daily_log()
    bucket = _today_bucket(log)
    if bucket["submitted_count"] >= DAILY_MAX_ORDERS or \
       bucket["submitted_notional"] + intent.notional > DAILY_MAX_NOTIONAL:
        bucket["deferred"].append(asdict(intent))
        _save_daily_log(log)
        result.deferred.append(intent)
        return result

    before = len(result.submitted)
    _submit_intent(broker, intent, result)
    if len(result.submitted) > before:
        bucket["submitted_count"] += 1
        bucket["submitted_notional"] += intent.notional
    _save_daily_log(log)
    return result
```

Now extend `execute_plan` to route large orders into the queue. **Replace** the inner loop body (the part that handles each intent after the cap checks) so that large orders go to pending instead of the broker. The full updated `execute_plan` body:

```python
def execute_plan(plan: OrderPlan, *, broker, reason: str) -> ExecutionResult:
    """Runs every intent through: HALT → market-open → daily caps → large-order gate."""
    result = ExecutionResult()
    intents = list(plan.sells) + list(plan.buys)

    if os.path.exists(HALT_PATH):
        for i in intents:
            result.skipped.append((i, "HALT file present"))
        return result

    log = _load_daily_log()
    bucket = _today_bucket(log)
    pending = _load_pending()
    now = dt.datetime.now(dt.UTC)

    for i in intents:
        # Daily cap first — deferred doesn't waste a pending slot
        if bucket["submitted_count"] >= DAILY_MAX_ORDERS:
            result.deferred.append(i)
            bucket["deferred"].append(asdict(i))
            continue
        if bucket["submitted_notional"] + i.notional > DAILY_MAX_NOTIONAL:
            result.deferred.append(i)
            bucket["deferred"].append(asdict(i))
            continue

        # Large-order gate
        if i.notional >= LARGE_ORDER_THRESHOLD:
            pending.append(_intent_to_pending(i, now))
            result.queued.append(i)
            continue

        before = len(result.submitted)
        _submit_intent(broker, i, result)
        if len(result.submitted) > before:
            bucket["submitted_count"] += 1
            bucket["submitted_notional"] += i.notional

    _save_daily_log(log)
    _save_pending(pending)
    return result
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): large-order pending queue + approve/reject/list helpers"
```

---

### Task 14: submit_exit

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── submit_exit ─────────────────────────────────────────────────

def test_submit_exit_sells_full_position(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    old = {
        "synced_at": "2026-04-16T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "TQQQ", "shares": 50.0, "avg_entry": 60.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "aggressive", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 10_000)

    from orders import submit_exit
    fb = FakeBroker()
    fb.seed_position("TQQQ", qty=50, avg_entry=60, mv=3000)

    result = submit_exit("TQQQ", reason="macro→contraction", broker=fb)
    assert len(result.submitted) == 1
    o = result.submitted[0]
    assert o.symbol == "TQQQ" and o.side == "sell"


def test_submit_exit_respects_halt(tmp_path, monkeypatch):
    _safety_paths(tmp_path, monkeypatch)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "TQQQ", "shares": 50.0, "avg_entry": 60.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "aggressive", "entry_reason": "x",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    })
    (tmp_path / "HALT").touch()

    from orders import submit_exit
    fb = FakeBroker()
    result = submit_exit("TQQQ", reason="macro→contraction", broker=fb)
    assert result.submitted == []
    assert any("HALT" in msg for _, msg in result.skipped)
```

- [ ] **Step 2: Run — expect failures**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```

- [ ] **Step 3: Implement `submit_exit`**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── submit_exit ─────────────────────────────────────────────────

def submit_exit(symbol: str, *, reason: str, broker) -> ExecutionResult:
    """Full-position exit routed through the same safety rails as a plan."""
    cache = _load_portfolio_cache()
    meta = next((p for p in cache.get("positions", []) if p["symbol"] == symbol), None)
    if meta is None:
        result = ExecutionResult()
        result.skipped.append((None, f"no cached metadata for {symbol}"))  # type: ignore[arg-type]
        return result

    tranche = meta.get("tranche", "unknown")
    notional = float(meta["market_value"])
    cid = _make_cid(tranche, f"exit-{reason[:16]}", symbol, dt.date.today())
    intent = OrderIntent(
        symbol=symbol, notional=notional, side="sell",
        reason=reason, tranche=tranche, client_order_id=cid,
    )
    # Wrap in a one-buy plan so HALT + caps + large-order logic fires uniformly.
    # "buys=[intent]" is a shape trick: execute_plan treats sells first, but
    # a single sell in `buys` vs `sells` is equivalent for routing.
    return execute_plan(OrderPlan(buys=[], sells=[intent], holds=[]),
                        broker=broker, reason=reason)
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): submit_exit routes through safety rails"
```

---

### Task 15: tag_position CLI helper

**Files:**
- Modify: `orders.py`
- Modify: `tests/test_orders.py`

- [ ] **Step 1: Write the failing test**

Append to `/Users/zl/works/stock/tests/test_orders.py`:

```python
# ── tag_position ────────────────────────────────────────────────

def test_tag_position_updates_metadata(tmp_path, monkeypatch):
    old = {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "NVDA", "shares": 5, "avg_entry": 100,
             "market_value": 520, "unrealized_pl": 0,
             "tranche": "unknown", "entry_reason": "external",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": None},
                     "aggressive": {"last_rebalance": None}},
    }
    _portfolio_cache(tmp_path, monkeypatch, old)

    from orders import tag_position
    tag_position("NVDA", tranche="core", entry_reason="manual 2026-04-17")

    got = json.loads((tmp_path / "portfolio.json").read_text())
    pos = got["positions"][0]
    assert pos["tranche"] == "core"
    assert pos["entry_reason"] == "manual 2026-04-17"


def test_tag_position_bad_tranche_raises(tmp_path, monkeypatch):
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [], "tranches": {"core": {"last_rebalance": None},
                                       "aggressive": {"last_rebalance": None}},
    })
    from orders import tag_position
    with pytest.raises(ValueError):
        tag_position("NVDA", tranche="invalid")
```

- [ ] **Step 2: Run — expect failure**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```

- [ ] **Step 3: Implement `tag_position`**

Append to `/Users/zl/works/stock/orders.py`:

```python
# ── tag_position ────────────────────────────────────────────────

def tag_position(symbol: str, tranche: str, entry_reason: str = "manual") -> None:
    """Set tranche/entry_reason for a position in the cache.
    Use to label an 'unknown' position after a manual Alpaca trade.
    """
    if tranche not in ("core", "aggressive"):
        raise ValueError(f"tranche must be 'core' or 'aggressive', got {tranche!r}")

    cache = _load_portfolio_cache()
    found = False
    for p in cache.get("positions", []):
        if p["symbol"] == symbol:
            p["tranche"] = tranche
            p["entry_reason"] = entry_reason
            found = True
            break
    if not found:
        raise ValueError(f"{symbol} not in portfolio cache — run sync_state first")

    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(cache, f, indent=2, default=str)
```

- [ ] **Step 4: Run — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```
Expected: 23 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add orders.py tests/test_orders.py
git commit -m "feat(orders): tag_position CLI helper for unknown-tranche positions"
```

---

### Task 16: rebalancer.py — skeleton + arg parsing

**Files:**
- Create: `rebalancer.py`
- Create: `tests/test_rebalancer.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/zl/works/stock/tests/test_rebalancer.py`:

```python
"""rebalancer.py — end-to-end wiring tests with FakeBroker."""
import datetime as dt
import json
import pytest
from tests.fakes import FakeBroker


def _portfolio_cache(tmp_path, monkeypatch, data):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def _safety_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))


def test_rebalancer_dry_run_no_submits(tmp_path, monkeypatch, capsys):
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    from rebalancer import run

    fb = FakeBroker()
    run(tranche="core", dry_run=True, force=True, broker=fb,
        target_builder=lambda: ({"SPY": 1.0}, 10_000))
    out = capsys.readouterr().out
    assert "SPY" in out
    assert fb._submitted == []


def test_rebalancer_skips_when_not_due(tmp_path, monkeypatch):
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [],
        "tranches": {"core": {"last_rebalance": dt.date.today().isoformat()},
                     "aggressive": {"last_rebalance": None}},
    })
    _safety_paths(tmp_path, monkeypatch)
    from rebalancer import run

    fb = FakeBroker()
    submitted = run(tranche="core", dry_run=False, force=False, broker=fb,
                     target_builder=lambda: ({"SPY": 1.0}, 10_000))
    assert submitted is None   # skipped


def test_rebalancer_submits_when_forced(tmp_path, monkeypatch):
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 100_000)
    from rebalancer import run

    fb = FakeBroker()
    result = run(tranche="core", dry_run=False, force=True, broker=fb,
                  target_builder=lambda: ({"SPY": 1.0}, 10_000))
    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "SPY"
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_rebalancer.py -v
```

- [ ] **Step 3: Implement `rebalancer.py` — skeleton with injectable target builder**

Create `/Users/zl/works/stock/rebalancer.py`:

```python
#!/usr/bin/env python3
"""
Scheduled rebalancer entry point.

Usage:
  python3 rebalancer.py --tranche core         # core tranche, mode-specific cadence
  python3 rebalancer.py --tranche aggressive   # aggressive tranche, 7-day cadence
  python3 rebalancer.py --dry-run              # print plan, submit nothing
  python3 rebalancer.py --force                # skip the "is it rebalance day?" gate
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys
from typing import Callable, Optional

import config
import orders
from broker import Broker


# ── Target builders ─────────────────────────────────────────────

def _build_core_targets() -> tuple[dict[str, float], float]:
    """Compose core-tranche targets from momentum + screener + macro.
    Returns (targets_dict, tranche_capital_dollars)."""
    from momentum import generate_signals
    from macro import macro_risk_adjustment

    capital = config.INITIAL_CAPITAL * (1 - config.AGGRESSIVE_TRANCHE_PCT)
    macro_adj = macro_risk_adjustment(1.0)
    etf_pct = config.ETF_ALLOCATION_PCT * macro_adj
    stock_pct = config.STOCK_ALLOCATION_PCT * macro_adj
    # Remainder goes to BIL as macro hedge (captured as a target).
    safe_pct = max(0.0, 1.0 - etf_pct - stock_pct - config.CASH_BUFFER_PCT)

    signals = generate_signals()
    targets: dict[str, float] = {}
    for sym, w in signals["holdings"]:
        targets[sym] = targets.get(sym, 0.0) + w * etf_pct

    # Stock sleeve: top-3 by composite score
    from screener import screen_stocks
    df = screen_stocks()
    if df is not None and not df.empty:
        top = df.head(3)
        per = stock_pct / max(1, len(top))
        for _, row in top.iterrows():
            targets[row["ticker"]] = targets.get(row["ticker"], 0.0) + per

    if safe_pct > 0.01:
        targets[config.SAFE_HAVEN] = targets.get(config.SAFE_HAVEN, 0.0) + safe_pct

    return targets, capital


def _build_aggressive_targets() -> tuple[dict[str, float], float]:
    """Top-N leveraged ETFs by momentum, equal-weighted. Uses ALL leveraged ETFs
    from config._ETF_LEVERAGED regardless of PORTFOLIO_MODE, because the
    aggressive tranche is always leveraged-ETF-only."""
    import pandas as pd
    from data import fetch_prices
    from momentum import _momentum_score

    capital = config.INITIAL_CAPITAL * config.AGGRESSIVE_TRANCHE_PCT
    top_n = config.AGGRESSIVE_PARAMS["momentum_top_n"]
    cash_buf = config.AGGRESSIVE_PARAMS["cash_buffer_pct"]

    leveraged = config._ETF_LEVERAGED
    prices = fetch_prices(leveraged + [config.SAFE_HAVEN], period="1y")
    rows = []
    for t in leveraged:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < config.SMA_FILTER_PERIOD:
            continue
        sma = s.rolling(config.SMA_FILTER_PERIOD).mean().iloc[-1]
        if s.iloc[-1] < sma:
            continue  # absolute momentum filter
        rows.append((t, _momentum_score(s, config.MOMENTUM_LOOKBACK_MONTHS)))

    rows.sort(key=lambda r: r[1], reverse=True)
    top = rows[:top_n]
    targets: dict[str, float] = {}
    if not top:
        targets[config.SAFE_HAVEN] = 1.0 - cash_buf
    else:
        w = (1.0 - cash_buf) / len(top)
        for sym, _ in top:
            targets[sym] = w
    return targets, capital


_TARGET_BUILDERS = {
    "core": _build_core_targets,
    "aggressive": _build_aggressive_targets,
}


# ── Entry point ─────────────────────────────────────────────────

def run(
    *,
    tranche: str,
    dry_run: bool,
    force: bool,
    broker,
    target_builder: Optional[Callable[[], tuple[dict[str, float], float]]] = None,
) -> Optional[orders.ExecutionResult]:
    """Execute (or dry-run) the rebalance for the given tranche.

    Returns:
      - None if skipped (not a rebalance day and not forced)
      - ExecutionResult on a real run
      - ExecutionResult with only `buys`/`sells` and no `submitted` on dry-run
    """
    if tranche not in ("core", "aggressive"):
        raise ValueError(f"tranche must be core|aggressive, got {tranche!r}")

    snap = orders.sync_state(broker, alerts=[])

    if not force:
        last = snap.tranches.get(tranche, {}).get("last_rebalance")
        if last:
            last_date = dt.date.fromisoformat(last)
            elapsed = (dt.date.today() - last_date).days
            if elapsed < config.REBALANCE_DAYS[tranche]:
                print(f"[{tranche}] not due: {elapsed}d since last rebalance "
                      f"(cadence {config.REBALANCE_DAYS[tranche]}d). Exiting.")
                return None

    builder = target_builder or _TARGET_BUILDERS[tranche]
    targets, tranche_capital = builder()

    plan = orders.reconcile_to_targets(
        targets, tranche=tranche, snapshot=snap,
        tranche_capital=tranche_capital, today=dt.date.today(),
    )

    _print_plan(tranche, targets, tranche_capital, plan)

    if dry_run:
        return orders.ExecutionResult()

    result = orders.execute_plan(plan, broker=broker, reason=f"{tranche} rebalance")

    # Update last_rebalance in the cache
    cache = orders._load_portfolio_cache()
    cache.setdefault("tranches", {}).setdefault(tranche, {})["last_rebalance"] = \
        dt.date.today().isoformat()
    import json
    with open(orders.PORTFOLIO_PATH, "w") as f:
        json.dump(cache, f, indent=2, default=str)

    _print_result(result)
    return result


def _print_plan(tranche: str, targets: dict, capital: float, plan: orders.OrderPlan):
    print(f"\n── {tranche.upper()} rebalance plan (capital ${capital:,.2f}) ──")
    print(f"Targets: {targets}")
    for i in plan.buys:
        print(f"  BUY   {i.symbol:6s} ${i.notional:>10,.2f}   (stop={i.stop_pct} trail={i.trail_pct})")
    for i in plan.sells:
        print(f"  SELL  {i.symbol:6s} ${i.notional:>10,.2f}")
    if plan.holds:
        print(f"  HOLD  {', '.join(plan.holds)}")


def _print_result(result: orders.ExecutionResult):
    print(f"\nSubmitted: {len(result.submitted)}  "
          f"Queued (Telegram): {len(result.queued)}  "
          f"Deferred: {len(result.deferred)}  "
          f"Skipped: {len(result.skipped)}")
    for o in result.submitted:
        print(f"  ✓ {o.symbol} {o.side} ${o.notional} ({o.id})")
    for i in result.queued:
        print(f"  ⏳ {i.symbol} ${i.notional} queued for Telegram approval")
    for i, msg in result.skipped:
        sym = i.symbol if i is not None else "?"
        print(f"  ✗ {sym}: {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tranche", required=True, choices=["core", "aggressive"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    broker = Broker(env=config.ALPACA_ENV)
    run(tranche=args.tranche, dry_run=args.dry_run, force=args.force, broker=broker)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the rebalancer tests — expect pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_rebalancer.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Run the full suite — expect all green**

```bash
cd /Users/zl/works/stock && python3 -m pytest -v
```
Expected: 26 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/zl/works/stock
git add rebalancer.py tests/test_rebalancer.py
git commit -m "feat(rebalancer): cron entry point for core + aggressive rebalancing"
```

---

### Task 17: watchdog.py — refactor to use sync_state

**Files:**
- Modify: `watchdog.py`

Rewriting watchdog to get its positions from `orders.sync_state` instead of the old local `portfolio.json` format. Remove `load_portfolio`, `save_portfolio`, and `init_portfolio` — they are now obsolete (the spec section "Deleted/no-longer-authoritative" calls this out).

- [ ] **Step 1: Replace the top-of-file functions**

Open `/Users/zl/works/stock/watchdog.py`. Replace the block from the comment `# ── Portfolio tracking ──────────────────────────────────────────` through the end of `init_portfolio()` (approximately lines 29–76 of the original file) with:

```python
# ── Portfolio state (via orders.sync_state) ─────────────────────

import orders


def snapshot() -> orders.PortfolioSnapshot:
    """Pull a fresh PortfolioSnapshot from Alpaca (source of truth).
    Alerts about unknown positions and missing brackets are returned separately.
    """
    from broker import Broker
    import config
    broker = Broker(env=config.ALPACA_ENV)
    alerts: list[str] = []
    snap = orders.sync_state(broker, alerts=alerts)
    snapshot.last_alerts = alerts  # type: ignore[attr-defined]
    return snap

snapshot.last_alerts: list[str] = []   # type: ignore[attr-defined]
```

- [ ] **Step 2: Update the check_* functions to read the snapshot shape**

Each of `check_price_moves`, `check_portfolio_status`, `check_volume`, etc. currently takes a `portfolio` dict and iterates `portfolio["positions"]` with fields `ticker`, `shares`, `entry_price`, `tranche`. The snapshot positions use `symbol`, `shares`, `avg_entry`, `tranche`. Add a small adapter and rewrite call sites.

Append after the `snapshot()` function:

```python
def _as_legacy_positions(snap: orders.PortfolioSnapshot) -> list[dict]:
    """Map snapshot position dicts to the legacy fields the check_* functions expect."""
    return [
        {
            "ticker": p["symbol"],
            "shares": p["shares"],
            "entry_price": p["avg_entry"],
            "entry_date": "",
            "tranche": p.get("tranche", "core"),
        }
        for p in snap.positions
        if p.get("tranche") != "unknown"
    ]
```

Now find every call site that uses the old portfolio dict and replace it. Run:

```bash
cd /Users/zl/works/stock && grep -n "load_portfolio\|init_portfolio\|save_portfolio" watchdog.py
```

Expected output: a handful of lines, typically `portfolio = load_portfolio()` inside `main()` and `run_watchdog()`. For each such line:

**Replace** a line like `portfolio = load_portfolio()` with:

```python
snap = snapshot()
portfolio = {
    "positions": _as_legacy_positions(snap),
    "cash": snap.cash,
    "initial_capital": config.INITIAL_CAPITAL,
}
```

**Delete** any call to `save_portfolio(...)` — the cache is written inside `sync_state`.

**Delete** any `from watchdog import load_portfolio` inside the module itself (the `init_portfolio` / `load_portfolio` / `save_portfolio` top-level defs were removed in Step 1; leftover references will crash).

- [ ] **Step 3: Wire signal-driven exits through orders.submit_exit**

Find the macro-shift check in `watchdog.py`. Locate where a bearish regime is detected. Where it currently *reports* the shift, add an exit call for leveraged ETFs in the aggressive tranche.

Add this helper to `watchdog.py` (near the end, before `main`):

```python
def act_on_macro_flip(snap: orders.PortfolioSnapshot, regime: str) -> list[str]:
    """If macro regime turned bearish today, exit leveraged-ETF aggressive positions."""
    from broker import Broker
    import config

    if regime != "contraction":
        return []

    broker = Broker(env=config.ALPACA_ENV)
    notifications: list[str] = []
    for p in snap.by_tranche("aggressive"):
        sym = p["symbol"]
        if sym not in config._ETF_LEVERAGED:
            continue
        result = orders.submit_exit(sym, reason="macro-contraction", broker=broker)
        if result.submitted:
            notifications.append(f"Exited {sym} on macro flip.")
        for _, msg in result.skipped:
            notifications.append(f"Could not exit {sym}: {msg}")
    return notifications
```

Wire this into the existing `check_macro_shift` flow by calling it after the regime label is computed. If the file already has a clear macro-check path, add `act_on_macro_flip(snap, regime)` and merge its return into the alerts list.

- [ ] **Step 4: Remove stop-loss manual-exit logic**

Alpaca's bracket orders now fire stops. The watchdog's job reduces to *verification* (handled by `sync_state`'s "missing bracket" alert) plus price-move reporting. In `check_price_moves`, keep the reporting logs but **remove** any code that previously recommended a manual SELL action.

In practice: the CRITICAL alert text that says "SELL NOW" can stay — it's advisory; actual selling is on Alpaca's side or through `orders.submit_exit` from a signal check. No code removal needed beyond not calling any order-submitting code here.

- [ ] **Step 5: Remove `init_portfolio` invocation**

Search for any call to `init_portfolio()`:

```bash
cd /Users/zl/works/stock && grep -n "init_portfolio" watchdog.py run.py
```

Wherever it appears (likely in the `main()` bootstrap of `watchdog.py`), remove the call and the surrounding "if no portfolio exists" branch. If the portfolio cache is missing, `sync_state` will build it.

- [ ] **Step 6: Run watchdog with FakeBroker to smoke-test**

Create `/Users/zl/works/stock/tests/test_watchdog_smoke.py`:

```python
"""Smoke test: watchdog.snapshot() returns a PortfolioSnapshot via FakeBroker."""
import json
from tests.fakes import FakeBroker


def test_snapshot_returns_portfolio_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))

    # Patch broker construction to return FakeBroker
    fb = FakeBroker()
    fb.seed_position("SPY", 10, 500)
    monkeypatch.setattr("watchdog.Broker", lambda env: fb)

    from watchdog import snapshot
    snap = snapshot()
    assert snap.cash == 100_000
    assert any(p["symbol"] == "SPY" for p in snap.positions)
```

- [ ] **Step 7: Run**

```bash
cd /Users/zl/works/stock && python3 -m pytest -v
```
Expected: 27 passed.

- [ ] **Step 8: Commit**

```bash
cd /Users/zl/works/stock
git add watchdog.py tests/test_watchdog_smoke.py
git commit -m "refactor(watchdog): read state via orders.sync_state, route macro exits"
```

> **Note for cross-project:** the unimplemented `/Users/zl/works/tg-bot` plan (`docs/superpowers/plans/2026-04-11-telegram-bot.md`) imports `from watchdog import load_portfolio` and `check_portfolio_status(portfolio)`. Those break with this refactor. When the telegram-bot plan is implemented, its portfolio handler should use `watchdog.snapshot()` instead and read the `PortfolioSnapshot` fields directly. Update that plan doc accordingly, or it will need a fix on execution.

---

### Task 18: run.py — read-only reporter

**Files:**
- Modify: `run.py`

`run.py` currently ends in order synthesis (`run_portfolio_construction`). In the new world, orders come from `rebalancer.py`. `run.py` becomes "what would the system do right now?" read-only.

- [ ] **Step 1: Delete `run_portfolio_construction` and its call**

Open `/Users/zl/works/stock/run.py`. Remove the entire `def run_portfolio_construction(...)` function (approximately lines 150–277).

In `main()`, remove the line:

```python
run_portfolio_construction(signals, screen_df, macro)  # returns (etf, stocks, aggressive)
```

- [ ] **Step 2: Add a read-only holdings snapshot section**

Replace the removed call with:

```python
    # 3. Current holdings (read-only view from Alpaca)
    from broker import Broker, BrokerError
    try:
        broker = Broker(env=config.ALPACA_ENV if hasattr(config, "ALPACA_ENV") else "paper")
        acc = broker.get_account()
        positions = broker.get_positions()
        section("CURRENT ALPACA HOLDINGS")
        print(f"  Env:    {broker.env}")
        print(f"  Cash:   ${acc.cash:,.2f}")
        print(f"  Equity: ${acc.equity:,.2f}")
        for p in positions:
            pnl = p.unrealized_pl
            icon = "▲" if pnl >= 0 else "▼"
            print(f"    {p.symbol:6s}  {p.qty:>8.2f} × ${p.avg_entry:>8.2f} = "
                  f"${p.market_value:>10,.2f}  {icon} ${pnl:+,.2f}")
    except (BrokerError, Exception) as e:
        section("CURRENT ALPACA HOLDINGS")
        print(f"  (skipped: {e})")
```

- [ ] **Step 3: Remove the old "NEXT STEPS" block that told the user to place orders manually**

Replace the final block:

```python
    section("NEXT STEPS")
    print("  1. Review the recommended allocation above")
    print("  2. Place orders through your broker")
    print("  3. Set stop-loss orders at -8% per position")
    print("  4. Re-run this system monthly to rebalance")
    print("  5. Monitor regime changes (risk-on → risk-off)")
    print()
```

…with:

```python
    section("NEXT STEPS")
    print("  Recommendations above are read-only. To act on them:")
    print(f"    python3 rebalancer.py --tranche core --dry-run")
    print(f"    python3 rebalancer.py --tranche aggressive --dry-run")
    print(f"    # remove --dry-run when ready")
    print()
```

- [ ] **Step 4: Import fix for config**

At the top of `run.py` make sure we have `import config` (the original file imports specific names). Add:

```python
import config
```

near the other imports if not already present.

- [ ] **Step 5: Smoke-test**

Run:
```bash
cd /Users/zl/works/stock && python3 -c "import run; print('ok')"
```
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
cd /Users/zl/works/stock
git add run.py
git commit -m "refactor(run): strip order synthesis; become read-only reporter"
```

---

### Task 19: Integration tests — against Alpaca paper

**Files:**
- Create: `tests/test_integration.py`

These are `@pytest.mark.integration` and skipped by default. They exercise the real `Broker` against Alpaca's paper API. Requires `ALPACA_API_KEY` / `ALPACA_API_SECRET` in the environment.

- [ ] **Step 1: Create `tests/test_integration.py`**

Create `/Users/zl/works/stock/tests/test_integration.py`:

```python
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
```

- [ ] **Step 2: Verify unit tests still skip integration**

```bash
cd /Users/zl/works/stock && python3 -m pytest -v
```
Expected: 27 passed, integration tests skipped/deselected.

- [ ] **Step 3: Verify integration tests can run when env vars set**

Only run this if you have Alpaca paper keys:

```bash
cd /Users/zl/works/stock && ALPACA_API_KEY=... ALPACA_API_SECRET=... python3 -m pytest -m integration -v
```
Expected: all integration tests pass.

- [ ] **Step 4: Commit**

```bash
cd /Users/zl/works/stock
git add tests/test_integration.py
git commit -m "test: add Alpaca paper integration tests (opt-in)"
```

---

### Task 20: Documentation + onboarding

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Replace the Quick Start section**

Open `/Users/zl/works/stock/README.md`. Replace the **Quick Start** section (lines 5–16 in the original) with:

```markdown
## Quick Start

```bash
pip3 install -r requirements.txt

# Copy .env.example to .env and fill in:
#   FRED_API_KEY (free)                  https://fred.stlouisfed.org/docs/api/api_key.html
#   ALPACA_API_KEY + ALPACA_API_SECRET   https://app.alpaca.markets/paper/dashboard/overview
cp .env.example .env
# edit .env

# 1. See what the system would do (read-only)
python3 run.py

# 2. Dry-run the rebalancer (prints plan, submits nothing)
python3 rebalancer.py --tranche core --dry-run
python3 rebalancer.py --tranche aggressive --dry-run

# 3. When ready, let it place orders on the paper account
python3 rebalancer.py --tranche core
```
```

- [ ] **Step 2: Replace the Commands section**

Find the `## Commands` section and replace the "Full analysis" block with a new "Trading" subsection before the existing "Daily monitoring":

````markdown
```bash
# Trading (Alpaca; paper by default)
python3 rebalancer.py --tranche core              # core tranche, mode-specific cadence
python3 rebalancer.py --tranche aggressive        # aggressive tranche, weekly
python3 rebalancer.py --tranche core --dry-run    # plan without submitting
python3 rebalancer.py --tranche core --force      # bypass cadence gate

# Kill-switch
touch .cache/HALT                                  # pause all order logic
rm .cache/HALT                                     # resume

# Tag an externally-opened position so it's counted in a tranche
python3 -c "from orders import tag_position; tag_position('NVDA', 'core', 'manual 2026-04-17')"
```
````

- [ ] **Step 3: Add a new Safety Rails section after Risk Management**

Append after the "Risk Management" section:

```markdown
## Safety Rails

Every order — rebalance, stop-exit, or signal-driven — goes through `orders.py`:

1. **HALT file** (`.cache/HALT`) — if present, all order logic exits cleanly.
2. **Paper/live guard** — live mode requires both `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`.
3. **Daily caps** — `DAILY_MAX_ORDERS` and `DAILY_MAX_NOTIONAL` in `config.py`.
4. **Large-order approval** — orders ≥ `LARGE_ORDER_THRESHOLD` ($2K default) go to `pending_orders.json` and require Telegram approval before submission.

## Switching to live

Paper is the default. Before flipping to live:

1. Run on paper for several weeks. Review `daily_log.csv`, verify brackets always attach, watch for Telegram prompts that were wrong.
2. Set `DAILY_MAX_NOTIONAL` to a small number (e.g. $500) in `config.py`.
3. Export `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`.
4. Ramp `DAILY_MAX_NOTIONAL` up over subsequent weeks.
```

- [ ] **Step 4: Update the cron example**

In the "Daily Watchdog" / "Automate with cron" section, add a second cron line for the rebalancer:

```
30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
0  9 * * 1-5 cd /Users/zl/works/stock && python3 rebalancer.py --tranche core >> .cache/rebalance.log 2>&1
0  9 * * 1   cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive >> .cache/rebalance.log 2>&1
```

Note: rebalancer.py no-ops unless cadence is reached, so running daily is fine.

- [ ] **Step 5: Commit**

```bash
cd /Users/zl/works/stock
git add README.md
git commit -m "docs: update README for Alpaca workflow + safety rails"
```

---

### Task 21: Onboarding migration notes + cleanup

**Files:**
- No code changes; operational steps.

- [ ] **Step 1: Delete the legacy portfolio.json**

The current `portfolio.json` uses a different schema and is not needed once `sync_state` runs. Either:
- Delete it: `rm /Users/zl/works/stock/portfolio.json` (it's now gitignored).
- Or leave it; `sync_state` will overwrite it with the new schema on first run.

No git action — it's gitignored as of Task 1.

- [ ] **Step 2: Create `.cache/` if it doesn't exist**

```bash
mkdir -p /Users/zl/works/stock/.cache
```

- [ ] **Step 3: Final full test sweep**

```bash
cd /Users/zl/works/stock && python3 -m pytest -v
```
Expected: all unit tests pass; integration tests skipped.

- [ ] **Step 4: Verify `.gitignore` coverage**

```bash
cd /Users/zl/works/stock && git status --ignored
```
Expected: `portfolio.json`, `pending_orders.json`, `.cache/*` all listed as ignored.

- [ ] **Step 5: Final commit (if anything changed)**

```bash
cd /Users/zl/works/stock && git status
# If anything to commit:
# git add <files> && git commit -m "chore: finalize alpaca integration"
```

---

## Post-implementation checklist (manual, not automated)

These are the staged-rollout gates from the spec. Keep this list in the PR description:

- [ ] Week 1–2: paper, `rebalancer.py --dry-run` only. Read every plan.
- [ ] Week 3–4: paper, real submits enabled via cron. Telegram bot connected. Watch `daily_log.csv`.
- [ ] Week 5+: review paper performance vs. backtest expectations. Check for tranche drift > 5%, missed stops, wrong Telegram prompts.
- [ ] Flip to live: set `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`, set `DAILY_MAX_NOTIONAL=500` for the first week, ramp from there.
