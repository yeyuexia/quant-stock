# SEPA Take-Profit Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Minervini-style scale-out (2R sell 1/3, 3R sell 1/3) and 21EMA trailing exit on the remaining 1/3 for core-tranche positions only.

**Architecture:** New `sepa_exits.py` provides pure-compute decision helpers (`initial_r`, `r_multiple`, `next_r_tier_action`, `ma_break`, `ma_trail_should_exit`). `orders.sync_state` is extended to snapshot immutable per-position fields (`initial_entry_price`, `initial_qty`, `initial_stop_price`) on first sight and append `r_tier_filled` labels when the observed qty drops below tier thresholds. Two new side-effecting helpers in `orders.py` (`submit_partial_exit`, `cancel_position_trailing`) route through the existing `pending_plan` / executor path. A new `watchdog.check_sepa_exits` orchestrates the rules per core position each day.

**Tech Stack:** Python 3.9+, pandas, numpy, pytest, pytest-mock.

**Spec:** `docs/superpowers/specs/2026-05-17-sepa-take-profit-phase1-design.md`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `broker.py` | Modify | Add `stop_price: Optional[float] = None` to `Order` dataclass; populate from Alpaca SDK in `_to_order`. |
| `tests/fakes.py` | Modify | Same `Order` import — no logic change, but tests need to construct `Order` with `stop_price=…`. |
| `config.py` | Modify | Add `SEPA_ENABLED`, `SEPA_R_TIERS`, `SEPA_MA_PERIOD`, `SEPA_MA_TYPE`, `SEPA_MA_HISTORY`. |
| `sepa_exits.py` | Create | Pure-compute SEPA decision helpers. No I/O. |
| `tests/test_sepa_exits.py` | Create | Unit tests for the pure-compute module. |
| `orders.py` | Modify | Extend `sync_state` with initial-field snapshot + `r_tier_filled` append. Add `submit_partial_exit` and `cancel_position_trailing` helpers. |
| `tests/test_orders.py` | Modify | Add tests for the `sync_state` extension and the two new helpers. |
| `watchdog.py` | Modify | Add `check_sepa_exits(snap, broker)` orchestration + Telegram notify; wire into `run_watchdog`. |
| `tests/test_watchdog.py` | Create or extend | Integration tests for `check_sepa_exits` against FakeBroker. |

---

## Task 1: Expose stop_price on `broker.Order`

**Files:**
- Modify: `/Users/zl/works/stock/broker.py:37-47` (`Order` dataclass)
- Modify: `/Users/zl/works/stock/broker.py:349-360` (`_to_order`)
- Test: `/Users/zl/works/stock/tests/test_broker.py` (extend if file exists; otherwise add to `tests/test_orders.py`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_broker.py` (or `tests/test_orders.py` if `test_broker.py` doesn't exist):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_broker.py -k "stop_price" -v
```

Expected: TypeError or AssertionError on the missing `stop_price` field.

- [ ] **Step 3: Add the field to the Order dataclass**

In `broker.py`, replace the `Order` dataclass (lines 37-47):

```python
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
```

- [ ] **Step 4: Populate stop_price in _to_order**

In `broker.py`, replace `_to_order` (lines 349-360):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_broker.py -k "stop_price" -v
```

Expected: both tests pass.

- [ ] **Step 6: Run full broker test suite for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_broker.py tests/test_orders.py -v --tb=line
```

Expected: all green. `Order` instances constructed elsewhere keep working because the new field has a default.

- [ ] **Step 7: Commit**

```bash
git add broker.py tests/test_broker.py
git commit -m "$(cat <<'EOF'
feat: expose stop_price on broker.Order

Adds Optional[float] stop_price field (default None) to the Order
dataclass and populates it from the Alpaca SDK response in _to_order.
Needed for SEPA sync_state to read the bracket's initial stop price.
EOF
)"
```

---

## Task 2: SEPA `config.py` constants

**Files:**
- Modify: `/Users/zl/works/stock/config.py` (insert after the new `ATR_*` block from the ATR feature, ~line 96)

Plumbing-only task. No tests of its own; downstream tasks exercise the constants.

- [ ] **Step 1: Add constants to `config.py`**

Find the existing `ATR_PERIOD = 14` / `ATR_STOP_MULTIPLIER = 2.0` block at the top of `config.py` (added by the ATR feature). Insert immediately after:

```python

# ── SEPA take-profit (Phase 1: core tranche only) ────────────────
# R-multiple scale-out: at each tier, sell `fraction` of initial_qty.
# After the final tier fills, the trailing-stop is cancelled and the
# remaining position is exited when daily close < EMA(SEPA_MA_PERIOD).
SEPA_ENABLED = True
SEPA_R_TIERS = [(2.0, 1/3), (3.0, 1/3)]   # (R-multiple, fraction-of-initial-qty)
SEPA_MA_PERIOD = 21
SEPA_MA_TYPE = "ema"                       # "ema" | "sma"
SEPA_MA_HISTORY = "6mo"                    # data.fetch_prices period for the EMA
```

- [ ] **Step 2: Sanity check import**

```bash
cd /Users/zl/works/stock && python3 -c "
import config
assert config.SEPA_ENABLED is True
assert config.SEPA_R_TIERS == [(2.0, 1/3), (3.0, 1/3)]
assert config.SEPA_MA_PERIOD == 21
assert config.SEPA_MA_TYPE == 'ema'
assert config.SEPA_MA_HISTORY == '6mo'
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add SEPA Phase 1 config constants"
```

---

## Task 3: `sepa_exits.py` pure-compute module

**Files:**
- Create: `/Users/zl/works/stock/sepa_exits.py`
- Create: `/Users/zl/works/stock/tests/test_sepa_exits.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sepa_exits.py`:

```python
"""Unit tests for sepa_exits — pure-compute SEPA decision helpers."""
import math
import pandas as pd
import pytest

from sepa_exits import (
    initial_r, r_multiple, next_r_tier_action,
    ma_break, ma_trail_should_exit,
)


def _pos(**overrides):
    """Build a position dict matching portfolio.json schema."""
    base = {
        "symbol": "AAPL",
        "shares": 30,
        "avg_entry": 100.0,
        "market_value": 3000.0,
        "tranche": "core",
        "initial_entry_price": 100.0,
        "initial_qty": 30,
        "initial_stop_price": 92.0,
        "r_tier_filled": [],
    }
    base.update(overrides)
    return base


# ── initial_r ──────────────────────────────────────────────────

def test_initial_r_basic():
    assert initial_r(_pos()) == 8.0  # 100 - 92


def test_initial_r_missing_initial_entry_returns_none():
    assert initial_r(_pos(initial_entry_price=None)) is None


def test_initial_r_missing_initial_stop_returns_none():
    assert initial_r(_pos(initial_stop_price=None)) is None


def test_initial_r_zero_when_stop_equals_entry():
    assert initial_r(_pos(initial_stop_price=100.0)) == 0.0


# ── r_multiple ─────────────────────────────────────────────────

def test_r_multiple_at_2r():
    assert math.isclose(r_multiple(_pos(), current_price=116.0), 2.0)


def test_r_multiple_below_entry_is_negative():
    assert math.isclose(r_multiple(_pos(), current_price=96.0), -0.5)


def test_r_multiple_unknown_initial_returns_none():
    assert r_multiple(_pos(initial_entry_price=None), current_price=116.0) is None


def test_r_multiple_zero_r_returns_none():
    """When R==0 (stop == entry), R-multiple is undefined."""
    assert r_multiple(_pos(initial_stop_price=100.0), current_price=120.0) is None


# ── next_r_tier_action ─────────────────────────────────────────

def test_next_r_tier_action_2r_reached_empty_filled():
    assert next_r_tier_action(_pos(), current_price=116.0) == "2R"


def test_next_r_tier_action_3r_reached_with_2r_filled():
    p = _pos(r_tier_filled=["2R"])
    assert next_r_tier_action(p, current_price=124.0) == "3R"


def test_next_r_tier_action_below_2r_returns_none():
    assert next_r_tier_action(_pos(), current_price=110.0) is None


def test_next_r_tier_action_all_filled_returns_none():
    p = _pos(r_tier_filled=["2R", "3R"])
    assert next_r_tier_action(p, current_price=200.0) is None


def test_next_r_tier_action_3r_reached_but_2r_not_filled_returns_2r():
    """Gap-up: position never observed at 2/3 qty yet, so SEPA only triggers 2R first."""
    p = _pos(r_tier_filled=[])
    assert next_r_tier_action(p, current_price=130.0) == "2R"


def test_next_r_tier_action_no_initial_stop_returns_none():
    p = _pos(initial_stop_price=None)
    assert next_r_tier_action(p, current_price=200.0) is None


# ── ma_break ──────────────────────────────────────────────────

def _closes_with_last(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_ma_break_close_below_ema_true():
    # 22 bars rising to 110, then last bar drops to 100.
    vals = list(range(89, 111)) + [100.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="ema") is True


def test_ma_break_close_above_ema_false():
    # 22 bars steady at 100, then last bar at 105.
    vals = [100.0] * 22 + [105.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="ema") is False


def test_ma_break_insufficient_data_returns_none():
    s = _closes_with_last([100.0] * 10)
    assert ma_break(s, period=21, ma_type="ema") is None


def test_ma_break_sma_variant():
    # SMA path also exercised.
    vals = [100.0] * 22 + [50.0]
    s = _closes_with_last(vals)
    assert ma_break(s, period=21, ma_type="sma") is True


# ── ma_trail_should_exit ───────────────────────────────────────

def test_ma_trail_gated_by_final_tier():
    """Without 3R in r_tier_filled, even a clear MA break returns False."""
    p = _pos(r_tier_filled=["2R"])
    s = _closes_with_last(list(range(89, 111)) + [50.0])
    assert ma_trail_should_exit(p, s) is False


def test_ma_trail_triggers_after_final_tier_when_break():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last(list(range(89, 111)) + [50.0])
    assert ma_trail_should_exit(p, s) is True


def test_ma_trail_no_trigger_when_close_above_ema():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last([100.0] * 22 + [120.0])
    assert ma_trail_should_exit(p, s) is False


def test_ma_trail_insufficient_data_returns_false():
    p = _pos(r_tier_filled=["2R", "3R"])
    s = _closes_with_last([100.0] * 10)
    assert ma_trail_should_exit(p, s) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -v
```

Expected: ModuleNotFoundError on `from sepa_exits import …`.

- [ ] **Step 3: Implement `sepa_exits.py`**

Create `sepa_exits.py`:

```python
"""Mark Minervini SEPA exit rules — Phase 1 (R-multiple scale-out + EMA trail).

All functions are pure: side-effect free, no I/O, no broker access. Callers
fetch the data and feed it in.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

import config


def _tier_label(r_multiple_value: float) -> str:
    """Stable label derived from the R multiple, e.g. 2.0 → '2R'."""
    return f"{int(r_multiple_value)}R"


def initial_r(position: dict) -> Optional[float]:
    """R per share = initial_entry_price − initial_stop_price.

    Returns None if either initial field is missing.
    """
    entry = position.get("initial_entry_price")
    stop = position.get("initial_stop_price")
    if entry is None or stop is None:
        return None
    return float(entry) - float(stop)


def r_multiple(position: dict, current_price: float) -> Optional[float]:
    """(current_price − initial_entry_price) / R. None if R undefined or zero."""
    r = initial_r(position)
    if r is None or r == 0:
        return None
    entry = float(position["initial_entry_price"])
    return (float(current_price) - entry) / r


def next_r_tier_action(position: dict, current_price: float) -> Optional[str]:
    """Return the label of the next R-tier to action, or None.

    Iterates config.SEPA_R_TIERS in order; returns the first tier whose
    R-multiple has been reached AND whose label is not already in
    r_tier_filled. Returns None if no tier qualifies or R is undefined.
    """
    rm = r_multiple(position, current_price)
    if rm is None:
        return None
    filled = position.get("r_tier_filled") or []
    for r, _frac in config.SEPA_R_TIERS:
        label = _tier_label(r)
        if label in filled:
            continue
        if rm >= r:
            return label
        return None
    return None


def _final_tier_label() -> str:
    """Label of the last entry in SEPA_R_TIERS."""
    r, _ = config.SEPA_R_TIERS[-1]
    return _tier_label(r)


def ma_break(closes: pd.Series, period: int = 21, ma_type: str = "ema") -> Optional[bool]:
    """True if the most recent close < MA(period). None on insufficient data.

    `ma_type` "ema" uses pandas .ewm(span=period, adjust=False); "sma" uses
    rolling mean. period+1 bars required.
    """
    s = closes.dropna()
    if len(s) < period + 1:
        return None
    if ma_type == "ema":
        ma = s.ewm(span=period, adjust=False).mean().iloc[-1]
    elif ma_type == "sma":
        ma = s.rolling(period).mean().iloc[-1]
    else:
        raise ValueError(f"unknown ma_type: {ma_type!r}")
    return float(s.iloc[-1]) < float(ma)


def ma_trail_should_exit(position: dict, closes: pd.Series) -> bool:
    """True only when r_tier_filled contains the final tier AND ma_break is True.

    Returns False (not None) when gating conditions aren't met — this is the
    "do nothing" signal, distinct from data-unavailable (also False here).
    """
    filled = position.get("r_tier_filled") or []
    if _final_tier_label() not in filled:
        return False
    broke = ma_break(closes, period=config.SEPA_MA_PERIOD, ma_type=config.SEPA_MA_TYPE)
    return broke is True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -v
```

Expected: all 19 tests pass.

- [ ] **Step 5: Commit**

```bash
git add sepa_exits.py tests/test_sepa_exits.py
git commit -m "$(cat <<'EOF'
feat: add sepa_exits module (R-multiple + EMA trail pure compute)

Pure-compute helpers for SEPA Phase 1 — no I/O, no broker access.
Callers feed in positions and price/close series and read back action
labels. Consumed next by orders.sync_state and watchdog.check_sepa_exits.
EOF
)"
```

---

## Task 4: `orders.sync_state` schema extension

**Files:**
- Modify: `/Users/zl/works/stock/orders.py:114-196` (`sync_state` function)
- Modify: `/Users/zl/works/stock/tests/test_orders.py` (extend with new sync_state tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── sync_state SEPA fields ──────────────────────────────────────

def _seed_stop_order(fb, symbol: str, stop_price: float, qty: float = 30.0,
                     parent_id: str = "parent_1"):
    """Attach a fake bracket stop-loss leg for symbol."""
    from broker import Order
    fb.seed_open_order(Order(
        id=f"stop_{symbol}", symbol=symbol, side="sell", type="stop",
        qty=qty, notional=None, status="accepted",
        client_order_id=f"stop-cid-{symbol}", parent_order_id=parent_id,
        stop_price=stop_price,
    ))


def test_sync_state_snapshots_initial_fields_on_first_seen_core_position(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30.0
    assert p["initial_stop_price"] == 92.0
    assert p["r_tier_filled"] == []


def test_sync_state_preserves_initial_fields_across_runs(tmp_path, monkeypatch):
    """Once snapshotted, initial_* fields are never re-written."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    # avg_entry has drifted to 105 (added to position), stop replaced at 95.
    fb.seed_position("AAPL", qty=40, avg_entry=105.0, mv=4200.0)
    _seed_stop_order(fb, "AAPL", stop_price=95.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    # Initial fields are immutable:
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30
    assert p["initial_stop_price"] == 92.0


def test_sync_state_initial_stop_none_when_no_open_stop_order(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    # No stop order seeded.

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["initial_entry_price"] == 100.0
    assert p["initial_qty"] == 30.0
    assert p["initial_stop_price"] is None
    assert p["r_tier_filled"] == []


def test_sync_state_appends_r_tier_when_qty_drops_to_two_thirds(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=20, avg_entry=100.0, mv=2400.0)  # 2/3 of 30
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R"]


def test_sync_state_appends_r_tier_3R_when_qty_drops_to_one_third(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 20.0, "avg_entry": 100.0,
             "market_value": 2400.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": ["2R"]},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=10, avg_entry=100.0, mv=1200.0)  # 1/3 of 30
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R", "3R"]


def test_sync_state_appends_both_tiers_when_qty_drops_in_one_step(tmp_path, monkeypatch):
    """Gap-up partial-sell scenario: r_tier_filled went [] → ["2R", "3R"] in one sync."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=10, avg_entry=100.0, mv=1200.0)  # straight to 1/3
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == ["2R", "3R"]


def test_sync_state_does_not_append_r_tier_on_full_qty(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": []},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)  # unchanged
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["r_tier_filled"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "sync_state and (initial or r_tier or sepa)" -v
```

Expected: 7 tests fail because the new keys are not yet written by `sync_state`.

- [ ] **Step 3: Extend `sync_state` in `orders.py`**

In `orders.py`, locate the `sync_state` function (lines 114-196). Replace the position-building loop (lines 144-171) with:

```python
    # Index stop orders for initial_stop_price lookup. The bracket's stop-loss
    # leg type is "stop"; standalone "stop_loss" type maps the same way.
    stop_orders_by_symbol: dict[str, Order] = {}
    for o in open_orders:
        if o.type in ("stop", "stop_loss"):
            stop_orders_by_symbol[o.symbol] = o

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
        if stop_id is None and trail_id is None:
            alerts.append(f"No bracket/trailing stop attached to {p.symbol} — "
                          "stop protection inactive.")

        # ── SEPA initial-field snapshot ─────────────────────────
        existing_initial = (meta or {}).get("initial_entry_price")
        if existing_initial is None:
            # First sight (or pre-SEPA cache): snapshot now.
            initial_entry_price = float(p.avg_entry)
            initial_qty = float(p.qty)
            stop_ord = stop_orders_by_symbol.get(p.symbol)
            initial_stop_price = (
                float(stop_ord.stop_price)
                if stop_ord is not None and stop_ord.stop_price is not None
                else None
            )
            r_tier_filled: list[str] = []
        else:
            # Immutable: preserve initial_*; check r_tier_filled appends below.
            initial_entry_price = (meta or {}).get("initial_entry_price")
            initial_qty = (meta or {}).get("initial_qty")
            initial_stop_price = (meta or {}).get("initial_stop_price")
            r_tier_filled = list((meta or {}).get("r_tier_filled", []))

            if (initial_qty and float(initial_qty) > 0
                    and initial_stop_price is not None
                    and tranche == "core"):
                EPS = 1.0  # 1-share tolerance for fractional shares
                cumulative_frac = 0.0
                for r, frac in config.SEPA_R_TIERS:
                    cumulative_frac += frac
                    label = f"{int(r)}R"
                    if label in r_tier_filled:
                        continue
                    threshold = float(initial_qty) * (1.0 - cumulative_frac) + EPS
                    if float(p.qty) <= threshold:
                        r_tier_filled.append(label)
                    else:
                        break

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
            "initial_entry_price": initial_entry_price,
            "initial_qty": initial_qty,
            "initial_stop_price": initial_stop_price,
            "r_tier_filled": r_tier_filled,
        })
```

(`Order` is already imported at the top of `orders.py` via `from broker import (...`.)

- [ ] **Step 4: Run the SEPA-extension tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "sync_state and (initial or r_tier or sepa)" -v
```

Expected: 7 tests pass.

- [ ] **Step 5: Run the full sync_state test set to confirm no regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "sync_state" -v --tb=line
```

Expected: all green (existing `test_sync_state_*` tests previously didn't assert on the new keys; they still pass because positions[] grew but old keys are intact).

- [ ] **Step 6: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "$(cat <<'EOF'
feat: sync_state snapshots SEPA initial_* fields + appends r_tier_filled

Per-position fields initial_entry_price, initial_qty, initial_stop_price
(immutable once set) and r_tier_filled (append-only on observed qty
drop) are now written by sync_state into portfolio.json. The stop price
comes from the bracket's open stop-loss leg via broker.Order.stop_price.
EOF
)"
```

---

## Task 5: `orders.submit_partial_exit` helper

**Files:**
- Modify: `/Users/zl/works/stock/orders.py` (add directly after `submit_exit`, around line 702)
- Modify: `/Users/zl/works/stock/tests/test_orders.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── submit_partial_exit ─────────────────────────────────────────

def _seed_cache_position(tmp_path, monkeypatch, symbol="AAPL", initial_qty=30,
                         current_qty=30, avg_entry=100.0, mv=3000.0,
                         r_tier_filled=None, initial_entry_price=100.0,
                         initial_stop_price=92.0):
    """Seed portfolio.json with one SEPA-ready position."""
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 5000.0, "equity": 50_000.0,
        "positions": [{
            "symbol": symbol, "shares": current_qty, "avg_entry": avg_entry,
            "market_value": mv, "unrealized_pl": 0.0,
            "tranche": "core", "entry_reason": "core rebalance",
            "stop_order_id": None, "trail_order_id": None,
            "initial_entry_price": initial_entry_price,
            "initial_qty": initial_qty,
            "initial_stop_price": initial_stop_price,
            "r_tier_filled": r_tier_filled if r_tier_filled is not None else [],
        }],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })


def test_submit_partial_exit_writes_to_pending_plan(tmp_path, monkeypatch):
    from orders import submit_partial_exit
    from pending_plan import load_plan

    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    # Stub baseline.capture_baseline so we don't fetch live market data.
    from baseline import Baseline
    monkeypatch.setattr("baseline.capture_baseline",
                        lambda: Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                                         captured_at="2026-05-10T14:00:00+00:00"))

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)

    assert len(result.queued) == 1
    intent = result.queued[0]
    assert intent.symbol == "AAPL"
    assert intent.side == "sell"
    # notional ≈ initial_qty * fraction * current_price = 30 * 1/3 * 116 = 1160
    assert abs(intent.notional - 1160.0) < 0.01
    assert intent.reason == "sepa-2R"
    assert intent.tier == "HIGH"

    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and s.intent.reason == "sepa-2R"
               for s in plan.intents)


def test_submit_partial_exit_conflict_falls_back_to_direct(tmp_path, monkeypatch):
    """If pending_plan already has an AAPL intent, submit_partial_exit
    routes through execute_plan (the same pattern as submit_exit)."""
    from orders import submit_partial_exit
    from pending_plan import PendingPlan, IntentState, write_plan
    from baseline import Baseline

    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    # Pre-seed pending_plan with a conflicting AAPL intent.
    write_plan(PendingPlan(
        plan_id="conflict-1",
        tranche="core",
        created_at=dt.datetime(2026, 5, 10, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                          captured_at="2026-05-10T14:00:00+00:00"),
        intents=[IntentState(intent=OrderIntent(
            symbol="AAPL", notional=1500.0, side="sell",
            reason="rebalance sell", tranche="core",
            client_order_id="cid-conflict-1",
        ))],
    ))

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    # Direct execute_plan path → result.submitted has the sell.
    assert len(result.submitted) == 1
    assert result.submitted[0].symbol == "AAPL"
    assert result.submitted[0].side == "sell"


def test_submit_partial_exit_skips_when_initial_qty_missing(tmp_path, monkeypatch):
    from orders import submit_partial_exit
    _seed_cache_position(tmp_path, monkeypatch, initial_qty=None)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert len(result.skipped) == 1


def test_submit_partial_exit_respects_halt(tmp_path, monkeypatch):
    from orders import submit_partial_exit, HALT_PATH

    halt = tmp_path / "HALT"
    halt.write_text("paused")
    monkeypatch.setattr("orders.HALT_PATH", str(halt))
    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    _seed_cache_position(tmp_path, monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)

    result = submit_partial_exit("AAPL", fraction_of_initial=1/3,
                                 reason="sepa-2R", broker=fb)
    assert result.submitted == []
    assert result.queued == []
    assert any("HALT" in msg for _, msg in result.skipped)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "submit_partial_exit" -v
```

Expected: ImportError on `from orders import submit_partial_exit`.

- [ ] **Step 3: Implement `submit_partial_exit` in `orders.py`**

Insert immediately after `submit_exit` (around line 702) in `orders.py`:

```python
def submit_partial_exit(symbol: str, *, fraction_of_initial: float,
                        reason: str, broker) -> ExecutionResult:
    """Partial-position exit. Notional = initial_qty × fraction × current_price.

    Writes a HIGH-tier intent (150 bps tolerance) to pending_plan.json so
    executor.py slices it through circuit breakers. Conflict with an
    existing pending_plan intent for the same symbol → falls back to
    direct execute_plan (mirrors submit_exit).

    No-ops (records skipped) when:
      - HALT file present
      - position metadata missing in cache
      - initial_qty missing (legacy / unsnapshotted)
    """
    from dataclasses import replace as _replace
    from pending_plan import load_plan, write_plan, IntentState, PendingPlan
    from baseline import capture_baseline

    result = ExecutionResult()
    if os.path.exists(HALT_PATH):
        intent = OrderIntent(
            symbol=symbol, notional=0.0, side="sell",
            reason=reason, tranche="core",
            client_order_id=_make_cid("core", f"partial-{reason[:12]}", symbol, dt.date.today()),
        )
        result.skipped.append((intent, "HALT file present"))
        return result

    cache = _load_portfolio_cache()
    meta = next((p for p in cache.get("positions", []) if p["symbol"] == symbol), None)
    if meta is None:
        result.skipped.append((None, f"no cached metadata for {symbol}"))  # type: ignore[arg-type]
        return result

    initial_qty = meta.get("initial_qty")
    if initial_qty is None or float(initial_qty) <= 0:
        result.skipped.append((None, f"{symbol}: initial_qty missing — cannot size partial"))  # type: ignore[arg-type]
        return result

    try:
        current_price = float(broker._latest_price(symbol))
    except Exception as e:
        result.skipped.append((None, f"{symbol}: latest price unavailable: {e}"))  # type: ignore[arg-type]
        return result

    notional = round(float(initial_qty) * float(fraction_of_initial) * current_price, 2)
    tranche = meta.get("tranche", "core")
    cid = _make_cid(tranche, f"partial-{reason[:12]}", symbol, dt.date.today())
    raw = OrderIntent(
        symbol=symbol, notional=notional, side="sell",
        reason=reason, tranche=tranche, client_order_id=cid,
    )

    existing = load_plan()
    if existing is not None and any(s.intent.symbol == symbol for s in existing.intents):
        return execute_plan(
            OrderPlan(buys=[], sells=[raw], holds=[]),
            broker=broker, reason=reason,
        )

    tolerance = config.MACRO_EXIT_TOLERANCE_BPS / 10_000.0
    floor = round(current_price * (1 - tolerance), 4)
    priced = _replace(raw, tier="HIGH", decision_price=current_price,
                      max_price=floor, slice_count=2)

    if existing is None:
        baseline = capture_baseline()
        existing = PendingPlan(
            plan_id=f"sepa-{dt.date.today().isoformat()}",
            tranche=tranche,
            created_at=dt.datetime.now(dt.timezone.utc),
            baseline=baseline,
            intents=[IntentState(intent=priced)],
        )
    else:
        existing.intents.append(IntentState(intent=priced))

    write_plan(existing)
    result.queued.append(priced)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "submit_partial_exit" -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "$(cat <<'EOF'
feat: orders.submit_partial_exit — fraction-based partial sell

Sizes notional from initial_qty × fraction × current_price, writes a
HIGH-tier intent to pending_plan.json so executor.py slices it under
circuit breakers. Conflicts with existing same-symbol intents route
through direct execute_plan, matching submit_exit's pattern.
EOF
)"
```

---

## Task 6: `orders.cancel_position_trailing` helper

**Files:**
- Modify: `/Users/zl/works/stock/orders.py` (add immediately after `submit_partial_exit`)
- Modify: `/Users/zl/works/stock/tests/test_orders.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── cancel_position_trailing ───────────────────────────────────

def test_cancel_position_trailing_cancels_open_trailing_order(tmp_path, monkeypatch):
    from orders import cancel_position_trailing
    from broker import Order

    fb = FakeBroker()
    fb.seed_open_order(Order(
        id="ord_trail_1", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-cid", parent_order_id=None,
    ))
    # Also a stop order — should NOT be cancelled.
    fb.seed_open_order(Order(
        id="ord_stop_1", symbol="AAPL", side="sell", type="stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="stop-cid", parent_order_id=None, stop_price=92.0,
    ))

    result = cancel_position_trailing("AAPL", broker=fb)
    assert "ord_trail_1" in fb._canceled
    assert "ord_stop_1" not in fb._canceled
    assert result.skipped == []


def test_cancel_position_trailing_noop_when_no_trailing(tmp_path, monkeypatch):
    from orders import cancel_position_trailing

    fb = FakeBroker()
    result = cancel_position_trailing("AAPL", broker=fb)
    assert fb._canceled == []
    assert result.submitted == []
    assert result.skipped == []


def test_cancel_position_trailing_respects_halt(tmp_path, monkeypatch):
    from orders import cancel_position_trailing
    from broker import Order

    halt = tmp_path / "HALT"
    halt.write_text("paused")
    monkeypatch.setattr("orders.HALT_PATH", str(halt))

    fb = FakeBroker()
    fb.seed_open_order(Order(
        id="ord_trail_1", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-cid", parent_order_id=None,
    ))

    result = cancel_position_trailing("AAPL", broker=fb)
    assert fb._canceled == []  # HALT prevented the cancel
    assert any("HALT" in msg for _, msg in result.skipped)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "cancel_position_trailing" -v
```

Expected: ImportError on `from orders import cancel_position_trailing`.

- [ ] **Step 3: Implement `cancel_position_trailing` in `orders.py`**

Insert immediately after `submit_partial_exit`:

```python
def cancel_position_trailing(symbol: str, *, broker) -> ExecutionResult:
    """Cancel any open trailing_stop orders on `symbol`. No-op when absent.

    Respects HALT. Bypasses daily-cap and large-order gates — cancellations
    carry no notional and should never queue for approval.
    """
    result = ExecutionResult()
    if os.path.exists(HALT_PATH):
        result.skipped.append((None, f"{symbol}: HALT file present, not cancelling trailing"))  # type: ignore[arg-type]
        return result

    try:
        open_orders = broker.get_open_orders()
    except BrokerError as e:
        result.skipped.append((None, f"cancel_position_trailing({symbol}): {e}"))  # type: ignore[arg-type]
        return result

    for o in open_orders:
        if o.symbol != symbol or o.type != "trailing_stop":
            continue
        try:
            broker.cancel_order(o.id)
        except BrokerError as e:
            result.skipped.append((None, f"cancel {o.id}: {e}"))  # type: ignore[arg-type]
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "cancel_position_trailing" -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "feat: orders.cancel_position_trailing — drop trailing-stop on demand"
```

---

## Task 7: `watchdog.check_sepa_exits` orchestration

**Files:**
- Modify: `/Users/zl/works/stock/watchdog.py` (add new function; do NOT yet wire into `run_watchdog`)
- Create or extend: `/Users/zl/works/stock/tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

Create or extend `tests/test_watchdog.py` (create if absent, prepend the standard imports):

```python
"""Integration tests for watchdog SEPA orchestration against FakeBroker."""
import datetime as dt
import json
import pandas as pd
import pytest

import config
from broker import Order
from tests.fakes import FakeBroker


def _portfolio_cache(tmp_path, monkeypatch, data):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def _seed_core_position(tmp_path, monkeypatch, **overrides):
    base = {
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3000.0, "unrealized_pl": 0.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }
    base.update(overrides)
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 5000.0, "equity": 50_000.0,
        "positions": [base],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })


def _make_snap(positions, cash=5000.0, equity=50_000.0):
    from orders import PortfolioSnapshot
    return PortfolioSnapshot(
        synced_at="2026-05-10T14:00:00+00:00",
        alpaca_env="paper", cash=cash, equity=equity,
        positions=positions,
        tranches={"core": {"last_rebalance": "2026-05-10"},
                  "aggressive": {"last_rebalance": None}},
    )


def _stub_baseline(monkeypatch):
    from baseline import Baseline
    monkeypatch.setattr("baseline.capture_baseline",
                        lambda: Baseline(spy=450.0, vix=14.0, macro_score=0.2,
                                         captured_at="2026-05-10T14:00:00+00:00"))


def _stub_fetch_prices(monkeypatch, symbol: str, closes_values: list):
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=len(closes_values), freq="B")
    df = pd.DataFrame({symbol: closes_values}, index=idx)
    monkeypatch.setattr("data.fetch_prices",
                        lambda tickers, period="2y": df)


# ── 2R path ────────────────────────────────────────────────────

def test_check_sepa_exits_2r_path(tmp_path, monkeypatch):
    """At 2R, partial-sell 1/3, cancel trailing, re-trail at 2/3 qty."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch)
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)  # 2R hit
    fb.seed_open_order(Order(
        id="trail_old", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="trail-old", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_old",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])

    notifications = check_sepa_exits(snap, fb)

    # Trailing cancelled
    assert "trail_old" in fb._canceled
    # New trailing submitted at 2/3 qty
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert len(new_trails) == 1
    assert new_trails[0].qty == pytest.approx(20.0, abs=0.01)
    # Partial sell queued to pending_plan
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and "sepa-2R" in s.intent.reason
               for s in plan.intents)
    # Telegram notification
    assert any("2R" in line for line in notifications)


# ── 3R path ────────────────────────────────────────────────────

def test_check_sepa_exits_3r_path(tmp_path, monkeypatch):
    """At 3R with 2R already filled, partial-sell 1/3, cancel trailing, NO re-trail."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch, shares=20.0,
                        market_value=2480.0, r_tier_filled=["2R"])
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 124.0)  # 3R hit
    fb.seed_open_order(Order(
        id="trail_2", symbol="AAPL", side="sell", type="trailing_stop",
        qty=20.0, notional=None, status="accepted",
        client_order_id="trail-2", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 20.0, "avg_entry": 100.0,
        "market_value": 2480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_2",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": ["2R"],
    }])

    notifications = check_sepa_exits(snap, fb)

    assert "trail_2" in fb._canceled
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert new_trails == []  # NO re-trail at 3R
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL" and "sepa-3R" in s.intent.reason
               for s in plan.intents)
    assert any("3R" in line for line in notifications)


# ── MA-break path ──────────────────────────────────────────────

def test_check_sepa_exits_ma_break_path(tmp_path, monkeypatch):
    """With r_tier_filled=['2R','3R'] and close < 21EMA, submit full exit."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("orders.PENDING_PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("pending_plan.PLAN_PATH", str(tmp_path / "pending_plan.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH",
                        str(tmp_path / "telegram.json"))
    _seed_core_position(tmp_path, monkeypatch, shares=10.0,
                        market_value=1100.0, r_tier_filled=["2R", "3R"])
    _stub_baseline(monkeypatch)
    # Steady rise to 110 over 22 bars, then a drop to 80 (well below EMA).
    _stub_fetch_prices(monkeypatch, "AAPL",
                       list(range(89, 111)) + [80.0])

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 110.0)

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 10.0, "avg_entry": 100.0,
        "market_value": 1100.0, "unrealized_pl": 100.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": ["2R", "3R"],
    }])

    notifications = check_sepa_exits(snap, fb)
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL"
               and "sepa-21EMA-break" in s.intent.reason
               for s in plan.intents)
    assert any("21EMA" in line for line in notifications)


# ── Guard paths ────────────────────────────────────────────────

def test_check_sepa_exits_skips_aggressive_tranche(tmp_path, monkeypatch):
    """Aggressive positions are bypassed entirely."""
    from watchdog import check_sepa_exits

    fb = FakeBroker()
    snap = _make_snap([{
        "symbol": "TQQQ", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 4000.0, "unrealized_pl": 1000.0,
        "tranche": "aggressive", "entry_reason": "agg rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 90.0, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []


def test_check_sepa_exits_skips_when_initial_stop_none(tmp_path, monkeypatch):
    from watchdog import check_sepa_exits

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 200.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 6000.0, "unrealized_pl": 3000.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": None, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []


def test_check_sepa_exits_disabled_when_config_off(tmp_path, monkeypatch):
    from watchdog import check_sepa_exits
    monkeypatch.setattr("config.SEPA_ENABLED", False)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 116.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3480.0, "unrealized_pl": 480.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
    }])
    notifications = check_sepa_exits(snap, fb)
    assert notifications == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -v
```

Expected: ImportError on `from watchdog import check_sepa_exits`.

- [ ] **Step 3: Implement `check_sepa_exits` in `watchdog.py`**

In `watchdog.py`, add this function. A good location is immediately before `act_on_macro_flip` (currently around line 241). Also add a small helper for Telegram notifications at the top of the new function block:

```python
# ── SEPA take-profit (Phase 1) ───────────────────────────────────

def _sepa_notify(message: str, lines: list) -> None:
    """Append a Telegram message; also push to the in-process `lines` list
    so the caller can include them in the watchdog alert summary."""
    lines.append(message)
    path = getattr(config, "TELEGRAM_NOTIFY_PATH", None)
    if not path:
        return
    import json as _json
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    existing = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = _json.load(f)
        except Exception:
            existing = []
    existing.append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "watchdog.sepa",
        "message": message,
    })
    with open(path, "w") as f:
        _json.dump(existing, f, indent=2, default=str)


def check_sepa_exits(snap: "orders.PortfolioSnapshot", broker) -> list:
    """SEPA Phase 1 driver. Returns notification lines for the alert summary.

    Per core position, in order:
      1. If next R-tier reached → submit_partial_exit (1/3 of initial_qty),
         cancel_position_trailing, re-trail at the remaining qty (unless this
         is the final tier — in which case no re-trail).
      2. If r_tier_filled contains the final tier label → check 21EMA;
         if close < EMA, submit_exit (full).
    """
    notifications: list = []
    if not getattr(config, "SEPA_ENABLED", False):
        return notifications

    import sepa_exits
    import data

    for pos in snap.by_tranche("core"):
        symbol = pos["symbol"]
        if pos.get("initial_stop_price") is None:
            continue
        try:
            current_price = float(broker._latest_price(symbol))
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: no latest price ({e})")
            continue

        # 1. R-multiple scale-out
        action = sepa_exits.next_r_tier_action(pos, current_price)
        if action is not None:
            # Fraction = the fraction associated with this tier label in SEPA_R_TIERS.
            frac = next(
                (f for (r, f) in config.SEPA_R_TIERS if f"{int(r)}R" == action),
                None,
            )
            if frac is None:
                continue

            partial_result = orders.submit_partial_exit(
                symbol, fraction_of_initial=frac,
                reason=f"sepa-{action}", broker=broker,
            )
            orders.cancel_position_trailing(symbol, broker=broker)

            # Re-trail unless this is the final tier label.
            final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
            if action != final_label:
                remaining_fraction = 1.0 - sum(
                    f for (r, f) in config.SEPA_R_TIERS
                    if f"{int(r)}R" in pos.get("r_tier_filled", []) or f"{int(r)}R" == action
                )
                new_qty = float(pos["initial_qty"]) * remaining_fraction
                from orders import _make_cid
                _, trail_pct = orders._tranche_stops("core")
                cid = _make_cid("core", f"sepa-trail-{action}", symbol, dt.date.today())
                try:
                    broker.submit_trailing_stop(symbol, qty=new_qty,
                                                trail_percent=trail_pct,
                                                client_order_id=cid)
                except Exception as e:
                    notifications.append(f"⚠ SEPA {symbol}: re-trail failed: {e}")

            sold_dollars = float(pos["initial_qty"]) * frac * current_price
            sold_shares = float(pos["initial_qty"]) * frac
            tail_msg = (" — trailing-stop removed, now MA-trailing"
                        if action == final_label else "")
            _sepa_notify(
                f"🎯 SEPA {action} hit — {symbol}\n"
                f"Sold ~{sold_shares:.2f} shares ≈ ${sold_dollars:,.0f} at ${current_price:.2f}"
                f"{tail_msg}",
                notifications,
            )
            continue  # Don't also check MA on the same run; next watchdog observes qty drop.

        # 2. 21EMA trail (only when final tier already filled)
        final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
        if final_label not in (pos.get("r_tier_filled") or []):
            continue
        try:
            prices = data.fetch_prices([symbol], period=config.SEPA_MA_HISTORY)
            closes = (prices[symbol] if symbol in prices.columns
                      else prices.iloc[:, 0]).dropna()
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed: {e}")
            continue
        if sepa_exits.ma_trail_should_exit(pos, closes):
            orders.submit_exit(symbol, reason="sepa-21EMA-break", broker=broker)
            _sepa_notify(
                f"📉 SEPA 21EMA break — {symbol}\n"
                f"Last close ${float(closes.iloc[-1]):.2f} below 21EMA; "
                f"exiting remaining shares.",
                notifications,
            )

    return notifications
```

Also ensure `import config`, `import orders`, `import datetime as dt` are at the top of `watchdog.py` (`config` and `orders` already exist; `dt` already exists per the existing watchdog code).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "$(cat <<'EOF'
feat: watchdog.check_sepa_exits — daily SEPA Phase 1 orchestration

Per core position: R-multiple scale-out (2R/3R partial sells with
trailing-stop coordination) and EMA-trail exit after the final tier
fills. Pure pass-through to submit_partial_exit / cancel_position_trailing
/ submit_exit; no policy beyond rule selection. Notifies Telegram on
every action.
EOF
)"
```

---

## Task 8: Wire `check_sepa_exits` into `watchdog.run_watchdog`

**Files:**
- Modify: `/Users/zl/works/stock/watchdog.py:407-498` (`run_watchdog`)

This task is the integration point — no new test infrastructure; existing watchdog smoke tests cover regression.

- [ ] **Step 1: Locate the insertion point**

In `watchdog.py`, find the section after `ensure_trailing_stops` runs (currently around line 425-433) and before `PORTFOLIO STATUS` is printed (line 436). The SEPA check belongs here: after trailing-stop maintenance, before the print-based summary, so its `submit_partial_exit` / cancellations are reflected in the snapshot the summary prints.

Find:

```python
    if trail_result.skipped:
        for pair in trail_result.skipped:
            sym = pair[0].symbol if pair[0] is not None else "?"
            print(f"    ! Could not attach trailing stop on {sym}: {pair[1]}")

    # Portfolio status
    header("PORTFOLIO STATUS")
```

Replace with:

```python
    if trail_result.skipped:
        for pair in trail_result.skipped:
            sym = pair[0].symbol if pair[0] is not None else "?"
            print(f"    ! Could not attach trailing stop on {sym}: {pair[1]}")

    # SEPA Phase 1 take-profit checks (R-multiple scale-out + 21EMA trail)
    header("SEPA EXITS")
    sepa_lines = check_sepa_exits(snap, broker)
    if not sepa_lines:
        print("  No SEPA actions today.")
    else:
        for line in sepa_lines:
            print(f"  {line}")

    # Portfolio status
    header("PORTFOLIO STATUS")
```

- [ ] **Step 2: Sanity import**

```bash
cd /Users/zl/works/stock && python3 -c "import watchdog; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Run the full test suite for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest --tb=line
```

Expected: all green. Tally includes the new tests from Tasks 1, 3, 4, 5, 6, 7.

- [ ] **Step 4: Smoke check (read-only path — does not require Alpaca creds)**

```bash
cd /Users/zl/works/stock && python3 -c "
# Construct a fake snap with no positions to exercise check_sepa_exits's no-op path.
from orders import PortfolioSnapshot
from tests.fakes import FakeBroker
from watchdog import check_sepa_exits
snap = PortfolioSnapshot(synced_at='2026-05-17T14:00:00Z', alpaca_env='paper',
                        cash=0.0, equity=0.0, positions=[],
                        tranches={'core': {'last_rebalance': None},
                                  'aggressive': {'last_rebalance': None}})
notifications = check_sepa_exits(snap, FakeBroker())
print(notifications)
"
```

Expected output: `[]`.

- [ ] **Step 5: Commit**

```bash
git add watchdog.py
git commit -m "feat: wire check_sepa_exits into watchdog.run_watchdog"
```

---

## Self-Review Notes (filled out during plan authoring)

**Spec coverage:**
- §2 Goals — R-multiple scale-out → Task 7 (and prerequisites Tasks 1, 3, 5). 21EMA trail → Task 7. Trailing-stop coordination → Tasks 6, 7. Idempotency → Task 4 (sync_state qty-observation) + Task 5 (pending_plan conflict check). Telegram notify → Task 7. Core-only → Task 7 (`snap.by_tranche("core")`).
- §3 Architecture — Tasks 1 (broker.stop_price), 3 (sepa_exits), 4 (sync_state), 5 (submit_partial_exit), 6 (cancel_position_trailing), 7 (watchdog.check_sepa_exits), 8 (wire-up).
- §4.1 portfolio.json schema extension → Task 4.
- §4.2 sepa_exits.py functions → Task 3.
- §4.3 submit_partial_exit → Task 5.
- §4.4 cancel_position_trailing → Task 6.
- §4.5 watchdog.check_sepa_exits → Task 7.
- §4.6 config additions → Task 2.
- §5 state machine — Task 4 (r_tier_filled rules) + Task 7 (action selection).
- §6 edge cases — Task 5 (conflict, initial_qty missing, HALT), Task 6 (HALT), Task 7 (initial_stop_price=None, SEPA_ENABLED off, aggressive-tranche skip).
- §7 notifications format → Task 7 (`_sepa_notify` emits the documented multi-line shape).
- §8 testing — Task 3 (sepa_exits tests), Task 4 (sync_state tests), Tasks 5 and 6 (orders tests), Task 7 (watchdog integration).
- §10 out-of-scope — none added.

**Placeholder scan:** no TBD/TODO/placeholder phrases. Every code step shows the full code an engineer would paste.

**Type consistency:** `sepa_exits.atr` not defined (correct — only atr lives in `indicators.py`); SEPA function signatures defined in Task 3 are used identically in Task 7's `check_sepa_exits`. `config.SEPA_R_TIERS` introduced in Task 2 is consumed in Tasks 3, 4, and 7 in the same `(r, frac)` tuple shape. `broker.Order.stop_price` introduced in Task 1 is read by Task 4's `sync_state` extension. `orders.submit_partial_exit(symbol, *, fraction_of_initial, reason, broker)` keyword signature defined in Task 5 is used identically in Task 7.
