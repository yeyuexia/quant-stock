# ATR-Based Stop-Loss Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the core tranche's fixed-percentage initial stop-loss with `min(STOP_LOSS_PCT, 2 × ATR(14) / last_close)`, leaving aggressive tranche and trailing stops untouched.

**Architecture:** New `indicators.py` module provides pure-compute `atr()` (Wilder smoothing). A new `_effective_stop_pct(symbol, tranche)` helper in `orders.py` fetches OHLCV via `data.fetch_ohlcv`, calls `indicators.atr`, and returns `min(base, 2×ATR/last_close)` for core entries (or the unchanged base for aggressive). `reconcile_to_targets` calls the helper when building buy intents. Two new `config.py` constants (`ATR_PERIOD`, `ATR_STOP_MULTIPLIER`) plus quant-override allowlist entry for the multiplier.

**Tech Stack:** Python 3.9+, pandas, numpy, pytest, pytest-mock.

**Spec:** `docs/superpowers/specs/2026-05-17-atr-stop-loss-design.md`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `indicators.py` | Create | Pure-compute technical indicators. Initial content: `atr()`. |
| `tests/test_indicators.py` | Create | Unit tests for `atr()`. |
| `config.py` | Modify | Add `ATR_PERIOD`, `ATR_STOP_MULTIPLIER`; extend `_OVERRIDE_SCHEMA`. |
| `quant/applier.py` | Modify | Add `ATR_STOP_MULTIPLIER` to `_LOW_RISK_NUMERIC`. |
| `quant/trigger_prompt.md` | Modify | Document new overrideable key; update `STOP_LOSS_PCT` description as a cap. |
| `orders.py` | Modify | Add `_effective_stop_pct(symbol, tranche)`; thread into `reconcile_to_targets`. |
| `tests/test_orders.py` | Modify | Add helper tests + integration test through `reconcile_to_targets`. |

---

## Task 1: `indicators.py` with `atr()`

**Files:**
- Create: `/Users/zl/works/stock/indicators.py`
- Create: `/Users/zl/works/stock/tests/test_indicators.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_indicators.py` with:

```python
"""Unit tests for indicators.atr — Wilder-smoothed ATR(14)."""
import math
import numpy as np
import pandas as pd
import pytest

from indicators import atr


def _series(values):
    """Build a daily-indexed Series for the given values."""
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_atr_constant_true_range_returns_that_range():
    """20 bars where every TR = 1.0 → ATR converges to 1.0."""
    n = 20
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    # TR per bar: max(11-10=1, |11-10.5|=0.5, |10-10.5|=0.5) = 1.0 for every bar
    # except the very first which has no prev_close (uses high-low=1.0 anyway).
    result = atr(high, low, close, period=14)
    assert result is not None
    assert math.isclose(result, 1.0, rel_tol=1e-9)


def test_atr_insufficient_data_returns_none():
    """Fewer than period+1 bars → None."""
    high  = _series([11.0] * 10)
    low   = _series([10.0] * 10)
    close = _series([10.5] * 10)
    assert atr(high, low, close, period=14) is None


def test_atr_constant_price_returns_zero():
    """high == low == close throughout → TR=0 → ATR=0."""
    n = 20
    high = low = close = _series([100.0] * n)
    result = atr(high, low, close, period=14)
    assert result == 0.0


def test_atr_handles_nan_inputs():
    """NaN values in inputs do not crash; ATR is computed from the non-NaN tail."""
    n = 25
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    high.iloc[0]  = float("nan")
    low.iloc[0]   = float("nan")
    close.iloc[0] = float("nan")
    result = atr(high, low, close, period=14)
    # 24 valid bars remain; ATR is well-defined and equals 1.0 by the same
    # constant-TR argument as the first test.
    assert result is not None
    assert math.isclose(result, 1.0, rel_tol=1e-9)


def test_atr_wilder_step_matches_recurrence():
    """One-step Wilder update: ATR[t] = (ATR[t-1]*(n-1) + TR[t]) / n.

    Build 15 bars with constant TR=1, then a 16th bar with TR=2.
    Expected: ATR[14] = 1.0, ATR[15] = (1.0*13 + 2.0)/14 = 15/14.
    """
    n = 16
    # Bars 0..14: high-low = 1.0, close mid (TR=1.0)
    high  = _series([11.0] * n)
    low   = _series([10.0] * n)
    close = _series([10.5] * n)
    # Bar 15: widen so TR = 2.0 (high=12, low=10, prev_close=10.5 → high-low=2)
    high.iloc[-1]  = 12.0
    low.iloc[-1]   = 10.0
    close.iloc[-1] = 11.0
    result = atr(high, low, close, period=14)
    expected = (1.0 * 13 + 2.0) / 14
    assert result is not None
    assert math.isclose(result, expected, rel_tol=1e-9)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_indicators.py -v
```

Expected: ImportError / ModuleNotFoundError on `from indicators import atr`.

- [ ] **Step 3: Implement `indicators.py`**

Create `indicators.py`:

```python
"""Technical indicators. Pure compute — callers fetch the data.

Add new indicators here when more than one consumer needs them. Keep
implementations free of I/O so they can be unit-tested with synthetic input.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> Optional[float]:
    """Wilder-smoothed Average True Range. Returns the most-recent ATR value.

    Returns None when there is insufficient data (fewer than period+1 aligned
    non-NaN bars).
    """
    df = pd.DataFrame({"high": high, "low": low, "close": close}).dropna()
    if len(df) < period + 1:
        return None

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    # First TR has no prev_close — fall back to high - low for that bar.
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]

    # Initial ATR = simple mean of first `period` TR values.
    initial = tr.iloc[:period].mean()
    atr_val = float(initial)
    # Wilder smoothing for the remaining bars.
    for t in tr.iloc[period:]:
        atr_val = (atr_val * (period - 1) + float(t)) / period
    return atr_val
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_indicators.py -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add indicators.py tests/test_indicators.py
git commit -m "$(cat <<'EOF'
feat: add indicators.atr (Wilder-smoothed ATR(14))

Pure-compute helper, no I/O. Returns most-recent ATR value or None
on insufficient data. Used next by orders._effective_stop_pct.
EOF
)"
```

---

## Task 2: `config.py` constants + override allowlist

**Files:**
- Modify: `/Users/zl/works/stock/config.py` (insert after line 89, end of `AGGRESSIVE_PARAMS`; extend `_OVERRIDE_SCHEMA` around line 297)
- Modify: `/Users/zl/works/stock/quant/applier.py` (extend `_LOW_RISK_NUMERIC` around line 24)
- Modify: `/Users/zl/works/stock/quant/trigger_prompt.md` (add to low-risk allowlist, update `STOP_LOSS_PCT` description)

This task is plumbing-only — no tests of its own; downstream tasks exercise the constants.

- [ ] **Step 1: Add the constants to `config.py`**

Find the line `}` that closes `AGGRESSIVE_PARAMS = { ... }` (currently `config.py:89`). Insert immediately after:

```python

# ── Stop-loss ATR scaling (core tranche only) ───────────────────
# Initial stop = min(STOP_LOSS_PCT, ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close).
# Aggressive tranche keeps the fixed AGGRESSIVE_PARAMS["stop_loss_pct"].
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
```

- [ ] **Step 2: Add `ATR_STOP_MULTIPLIER` to `_OVERRIDE_SCHEMA`**

In `config.py`, locate the `_OVERRIDE_SCHEMA` block (currently line 293-315). Insert the new entry directly after the existing `STOP_LOSS_PCT` line (line 297) so the low-risk numerics stay grouped:

Find:
```python
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
```

Replace with:
```python
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "ATR_STOP_MULTIPLIER":  (float, 1.0,  4.0),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
```

- [ ] **Step 3: Add `ATR_STOP_MULTIPLIER` to `quant/applier.py:_LOW_RISK_NUMERIC`**

In `quant/applier.py`, locate `_LOW_RISK_NUMERIC` (lines 23-27). Replace with:

```python
_LOW_RISK_NUMERIC = {
    "STOP_LOSS_PCT":       (0.04, 0.20, 0.20),
    "ATR_STOP_MULTIPLIER": (1.0,  4.0,  0.25),
    "TRAILING_STOP_PCT":   (0.06, 0.25, 0.20),
    "CASH_BUFFER_PCT":     (0.02, 0.20, 0.50),
}
```

(`0.25` is the relative-pct band: the agent can propose values within ±25% of the current value AND inside `[1.0, 4.0]`.)

- [ ] **Step 4: Update `quant/trigger_prompt.md`**

Open `quant/trigger_prompt.md`. Find the low-risk allowlist block (around lines 46-51):

```markdown
- **Low-risk allowlist** (auto-applies if within bounds):
  - `WATCHLIST` — additions only, ≤100 total
  - `NEWS_SHOCK_KEYWORDS` — additions only, ≤30 total
  - `STOP_LOSS_PCT` — within ±20% of current AND in [0.04, 0.20]
  - `TRAILING_STOP_PCT` — within ±20% AND in [0.06, 0.25]
  - `CASH_BUFFER_PCT` — within ±50% AND in [0.02, 0.20]
```

Replace with:

```markdown
- **Low-risk allowlist** (auto-applies if within bounds):
  - `WATCHLIST` — additions only, ≤100 total
  - `NEWS_SHOCK_KEYWORDS` — additions only, ≤30 total
  - `STOP_LOSS_PCT` — within ±20% of current AND in [0.04, 0.20]. Note: this
    is now an **upper bound** on the per-symbol initial stop; the effective
    stop is `min(STOP_LOSS_PCT, ATR_STOP_MULTIPLIER × ATR(14) / last_close)`
    for core entries.
  - `ATR_STOP_MULTIPLIER` — within ±25% of current AND in [1.0, 4.0]. Scales
    the ATR contribution to the core-tranche initial stop. Lower = tighter
    stops on volatile names; higher = more breathing room.
  - `TRAILING_STOP_PCT` — within ±20% AND in [0.06, 0.25]
  - `CASH_BUFFER_PCT` — within ±50% AND in [0.02, 0.20]
```

- [ ] **Step 5: Sanity-check imports**

```bash
cd /Users/zl/works/stock && python3 -c "import config; print(config.ATR_PERIOD, config.ATR_STOP_MULTIPLIER)"
```

Expected output: `14 2.0`

```bash
cd /Users/zl/works/stock && python3 -c "from quant.applier import _LOW_RISK_NUMERIC; print('ATR_STOP_MULTIPLIER' in _LOW_RISK_NUMERIC)"
```

Expected output: `True`

- [ ] **Step 6: Commit**

```bash
git add config.py quant/applier.py quant/trigger_prompt.md
git commit -m "$(cat <<'EOF'
feat: add ATR_PERIOD / ATR_STOP_MULTIPLIER config + override allowlist

ATR_STOP_MULTIPLIER is low-risk (auto-apply within ±25% and [1.0, 4.0]).
STOP_LOSS_PCT description updated to reflect its new role as an upper
bound on the per-symbol initial stop.
EOF
)"
```

---

## Task 3: `orders._effective_stop_pct` helper

**Files:**
- Modify: `/Users/zl/works/stock/orders.py` (add helper directly after `_tranche_stops`, currently `orders.py:201-205`)
- Modify: `/Users/zl/works/stock/tests/test_orders.py` (append new tests at the end)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── _effective_stop_pct (ATR-scaled core stops) ─────────────────

def _ohlcv_constant(symbol: str, high: float, low: float, close: float, n: int = 30):
    """Build a MultiIndex OHLCV frame in the shape data.fetch_ohlcv returns."""
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        ("High",  symbol): [high]  * n,
        ("Low",   symbol): [low]   * n,
        ("Close", symbol): [close] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_effective_stop_pct_uses_atr_when_tighter(monkeypatch):
    """Low-vol data (ATR pct < base) → returns ATR-scaled pct."""
    from orders import _effective_stop_pct
    # high=100.5, low=99.5 → TR≈1, last_close=100 → ATR/close=0.01 → 2*ATR/close=0.02
    df = _ohlcv_constant("AAPL", high=100.5, low=99.5, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    # Expect 0.02 (< STOP_LOSS_PCT of 0.08 in balanced mode default)
    assert abs(result - 0.02) < 1e-6


def test_effective_stop_pct_caps_at_base(monkeypatch):
    """High-vol data (ATR pct > base) → returns STOP_LOSS_PCT."""
    import config
    from orders import _effective_stop_pct
    # TR≈20 on a $100 close → 2*ATR/close = 0.40 (> any base)
    df = _ohlcv_constant("TSLA", high=110.0, low=90.0, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("TSLA", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_aggressive_unchanged(monkeypatch):
    """Aggressive tranche short-circuits — does not call fetch_ohlcv."""
    import config
    from orders import _effective_stop_pct

    called = {"hit": False}
    def _trap(*a, **kw):
        called["hit"] = True
        raise AssertionError("fetch_ohlcv must not be called for aggressive")
    monkeypatch.setattr("data.fetch_ohlcv", _trap)

    result = _effective_stop_pct("TQQQ", "aggressive")
    assert result == config.AGGRESSIVE_PARAMS["stop_loss_pct"]
    assert called["hit"] is False


def test_effective_stop_pct_fallback_on_fetch_error(monkeypatch):
    """fetch_ohlcv raising → returns base, no exception escapes."""
    import config
    from orders import _effective_stop_pct

    def _boom(*a, **kw):
        raise RuntimeError("yfinance unavailable")
    monkeypatch.setattr("data.fetch_ohlcv", _boom)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_insufficient_data(monkeypatch):
    """Too few bars for ATR → returns base."""
    import config
    from orders import _effective_stop_pct
    df = _ohlcv_constant("AAPL", high=100.5, low=99.5, close=100.0, n=5)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_zero_atr(monkeypatch):
    """Constant prices → ATR=0 → fallback to base (not 0)."""
    import config
    from orders import _effective_stop_pct
    df = _ohlcv_constant("SHV", high=100.0, low=100.0, close=100.0, n=30)
    monkeypatch.setattr("data.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("SHV", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k effective_stop_pct -v
```

Expected: ImportError on `from orders import _effective_stop_pct` (function not yet defined).

- [ ] **Step 3: Add `_effective_stop_pct` to `orders.py`**

In `orders.py`, locate `_tranche_stops` (lines 201-205). Insert the new helper immediately after it:

```python
def _effective_stop_pct(symbol: str, tranche: str) -> float:
    """Per-symbol initial stop pct. ATR-scaled for core; fixed for aggressive.

    Returns min(STOP_LOSS_PCT, ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close)
    for core entries. Any data failure falls back to the tranche's base stop pct.
    """
    base, _ = _tranche_stops(tranche)
    if tranche != "core":
        return base
    try:
        import data
        import indicators
        ohlcv = data.fetch_ohlcv([symbol], period="1y")
        high  = ohlcv["High"][symbol].dropna()
        low   = ohlcv["Low"][symbol].dropna()
        close = ohlcv["Close"][symbol].dropna()
        atr_val = indicators.atr(high, low, close, period=config.ATR_PERIOD)
        last = float(close.iloc[-1]) if not close.empty else 0.0
        if atr_val is None or atr_val <= 0 or last <= 0:
            return base
        return min(base, config.ATR_STOP_MULTIPLIER * atr_val / last)
    except Exception:
        return base
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k effective_stop_pct -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "$(cat <<'EOF'
feat: orders._effective_stop_pct — ATR-scaled core-tranche stop

Computes min(STOP_LOSS_PCT, 2*ATR/last_close) for core entries via
data.fetch_ohlcv + indicators.atr. Aggressive returns the fixed
percentage without touching data. Any failure path falls back to the
configured percentage.

Not yet wired into reconcile_to_targets — that's the next commit.
EOF
)"
```

---

## Task 4: Wire `_effective_stop_pct` into `reconcile_to_targets`

**Files:**
- Modify: `/Users/zl/works/stock/orders.py:248-254` (buy-intent construction inside `reconcile_to_targets`)
- Modify: `/Users/zl/works/stock/tests/test_orders.py` (append integration test)

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_orders.py`:

```python
def test_reconcile_buy_uses_effective_stop(tmp_path, monkeypatch):
    """Buy intent's stop_pct comes from _effective_stop_pct, not _tranche_stops."""
    from orders import reconcile_to_targets

    # Force _effective_stop_pct to return a recognizable value.
    monkeypatch.setattr("orders._effective_stop_pct",
                        lambda sym, tranche: 0.037 if tranche == "core" else 0.10)

    snap = _snap(positions=[], cash=90_000, equity=90_000)
    plan = reconcile_to_targets(
        {"SPY": 1.0},
        tranche="core",
        snapshot=snap,
        tranche_capital=90_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 1
    assert plan.buys[0].symbol == "SPY"
    assert abs(plan.buys[0].stop_pct - 0.037) < 1e-9
    # trail_pct unchanged: still from _tranche_stops("core")
    import config
    assert abs(plan.buys[0].trail_pct - config.TRAILING_STOP_PCT) < 1e-9


def test_reconcile_aggressive_buy_uses_fixed_stop(tmp_path, monkeypatch):
    """Aggressive tranche keeps the fixed stop_loss_pct."""
    from orders import reconcile_to_targets
    import config

    snap = _snap(positions=[], cash=10_000, equity=10_000)
    plan = reconcile_to_targets(
        {"TQQQ": 1.0},
        tranche="aggressive",
        snapshot=snap,
        tranche_capital=10_000,
        today=dt.date(2026, 4, 17),
    )
    assert len(plan.buys) == 1
    assert abs(plan.buys[0].stop_pct
               - config.AGGRESSIVE_PARAMS["stop_loss_pct"]) < 1e-9
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "reconcile_buy_uses_effective_stop or reconcile_aggressive_buy_uses_fixed_stop" -v
```

Expected: `test_reconcile_buy_uses_effective_stop` fails because the buy intent's `stop_pct` is `config.STOP_LOSS_PCT` (e.g. 0.08), not 0.037. The aggressive test may pass already (aggressive already uses fixed stops) — but verify it does.

- [ ] **Step 3: Modify `reconcile_to_targets`**

In `orders.py`, locate the buy-intent block inside `reconcile_to_targets` (currently lines 248-254):

```python
        if diff > 0:
            cid = _make_cid(tranche, "rebalance", sym, today)
            buys.append(OrderIntent(
                symbol=sym, notional=round(diff, 2), side="buy",
                reason=reason, tranche=tranche, client_order_id=cid,
                stop_pct=stop_pct, trail_pct=trail_pct,
            ))
```

Replace with:

```python
        if diff > 0:
            cid = _make_cid(tranche, "rebalance", sym, today)
            buy_stop_pct = _effective_stop_pct(sym, tranche)
            buys.append(OrderIntent(
                symbol=sym, notional=round(diff, 2), side="buy",
                reason=reason, tranche=tranche, client_order_id=cid,
                stop_pct=buy_stop_pct, trail_pct=trail_pct,
            ))
```

(`stop_pct` from `_tranche_stops` is no longer used for buys; `trail_pct` still is. Leave the `_tranche_stops` call at the top of `reconcile_to_targets` — it still supplies `trail_pct`.)

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "reconcile_buy_uses_effective_stop or reconcile_aggressive_buy_uses_fixed_stop" -v
```

Expected: both pass.

- [ ] **Step 5: Run the entire reconcile + safety-rail test suite to confirm no regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -v
```

Expected: all tests pass. The existing `test_reconcile_opens_new_positions` and friends only assert `stop_pct is not None` (test_orders.py:153) — they remain green because `_effective_stop_pct` always returns a float. None of the existing assertions check the specific stop_pct value.

- [ ] **Step 6: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "$(cat <<'EOF'
feat: route core-tranche buys through _effective_stop_pct

reconcile_to_targets now calls _effective_stop_pct(sym, tranche) per
buy intent so the initial stop is ATR-scaled for core entries.
Aggressive intents unchanged; trail_pct continues to come from
_tranche_stops.
EOF
)"
```

---

## Task 5: Full-suite verification + smoke check

**Files:** none modified; verification only.

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/zl/works/stock && python3 -m pytest
```

Expected: all unit tests pass (integration tests `-m integration` are deselected by default per `pytest.ini`). If anything red appears, stop and investigate before proceeding.

- [ ] **Step 2: Smoke-check the read-only reporter**

```bash
cd /Users/zl/works/stock && python3 run.py 2>&1 | head -50
```

Expected: the report runs to completion without exceptions. No assertions to check — just that the system imports cleanly with the new `indicators` module and the modified `orders.py`.

- [ ] **Step 3: Smoke-check the rebalancer in dry-run**

```bash
cd /Users/zl/works/stock && python3 rebalancer.py --tranche core --dry-run --force 2>&1 | head -80
```

Expected: the plan prints with buy intents whose `stop=...` values may now vary per symbol (low-vol ETFs like SHY/BIL likely show tighter stops than `STOP_LOSS_PCT`; volatile stocks/leveraged ETFs cap at `STOP_LOSS_PCT`). No exceptions; no orders submitted (dry-run).

- [ ] **Step 4: Confirm aggressive tranche still uses fixed stop**

```bash
cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive --dry-run --force 2>&1 | head -40
```

Expected: every buy intent shows `stop=0.1` (== `AGGRESSIVE_PARAMS["stop_loss_pct"]`).

- [ ] **Step 5: Final commit (only if anything changed during verification)**

If steps 1–4 surfaced a fix, commit it. Otherwise skip this step — no empty commit.

```bash
git status
# If clean, no commit needed.
```

---

## Self-Review Notes (filled out during plan authoring)

**Spec coverage:**
- §2 Goals — per-symbol stop, graceful degradation, multiplier in override allowlist, dedicated indicators module → Tasks 1–3.
- §3 Architecture diagram → Task 3 + Task 4 wiring.
- §4.1 `indicators.py` → Task 1.
- §4.2 `_effective_stop_pct` → Task 3.
- §4.3 `reconcile_to_targets` change → Task 4.
- §4.4 `config.py` additions + override schema → Task 2.
- §5 Data flow + perf note → all tasks combined (perf optimization explicitly out of scope per spec).
- §6 Edge cases → Task 1 (atr None/zero) + Task 3 (fallback tests for fetch error, no data, zero ATR, aggressive short-circuit).
- §7 Watchdog non-change → no task (deliberate non-change; spec §7 documented).
- §8 Testing → Task 1 (4–5 indicator tests, including a Wilder-recurrence check beyond spec's list) + Task 3 (6 helper tests) + Task 4 (2 integration tests). Spec lists "test_atr_known_input"; covered by `test_atr_constant_true_range_returns_that_range` plus the Wilder-recurrence test.
- §9 Out-of-scope items → none added.

**Placeholder scan:** no TBD/TODO/placeholder phrases in any task. Every code block is complete and executable.

**Type consistency:** `atr(high, low, close, period=14) -> Optional[float]` defined in Task 1 and used identically in Task 3. `_effective_stop_pct(symbol, tranche) -> float` defined in Task 3 and patched in Task 4 test with the same signature. `config.ATR_PERIOD`, `config.ATR_STOP_MULTIPLIER` introduced in Task 2 and consumed in Task 3.
