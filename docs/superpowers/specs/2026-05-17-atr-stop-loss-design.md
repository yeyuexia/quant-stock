# ATR-Based Stop-Loss — Design

**Date:** 2026-05-17
**Status:** Spec, awaiting implementation plan
**Scope:** Replace the core tranche's fixed-percentage initial stop-loss with `min(STOP_LOSS_PCT, 2 × ATR(14) / last_close)`. Aggressive tranche and trailing-stop behavior are unchanged.

---

## 1. Motivation

Today every core-tranche position uses the mode-resolved `STOP_LOSS_PCT` as its initial stop (6% conservative / 8% balanced / 12% growth). That percentage is symbol-agnostic — a low-volatility ETF (e.g. SHY, where daily true range is ~0.1%) gets the same 8% buffer as a high-volatility name (where 8% might be inside one day's normal range).

A volatility-adapted stop uses the symbol's recent average true range to size the buffer:

- For calm names (`2 × ATR / price < STOP_LOSS_PCT`), the stop tightens — less drawdown per losing trade.
- For volatile names (`2 × ATR / price ≥ STOP_LOSS_PCT`), the stop is capped at the configured percentage — `STOP_LOSS_PCT` is now an upper bound, not a fixed value.

The min-rule means the **tighter** of the two stops always wins. This is a deliberate protective bias: prefer to be stopped out early on quiet names and let the existing percentage cap protect against degenerate cases.

---

## 2. Goals & non-goals

### Goals
- Per-symbol initial stop for core-tranche entries, derived from `min(STOP_LOSS_PCT, 2 × ATR(14) / last_close)`.
- Graceful degradation: any failure to compute ATR (missing data, fetch error, degenerate price) falls back to the existing `STOP_LOSS_PCT`.
- Multiplier exposed to the quant-review subagent's override allowlist (`ATR_STOP_MULTIPLIER`, bounded 1.0–4.0).
- Calculation lives in a new dedicated module so future indicators (RSI, MACD, etc.) have a home.

### Non-goals
- Aggressive-tranche stops (kept at fixed 10%).
- Trailing-stop logic (kept at fixed 12% core / 15% aggressive).
- Watchdog warning thresholds (kept symbol-agnostic; see §7).
- Migration of `screener._adr` into the new module (related concept but functioning code; YAGNI).
- Per-symbol stop recomputation after entry (initial-only; the trailing stop handles the dynamic side).

---

## 3. Architecture

```
reconcile_to_targets(targets, tranche="core")
  │
  └─ for each buy symbol:
        _effective_stop_pct(symbol, tranche)
          ├─ data.fetch_ohlcv([symbol], "1y")        ← existing cache
          ├─ indicators.atr(high, low, close, 14)    ← new module
          └─ return min(STOP_LOSS_PCT, k × ATR / last_close)
  │
  ▼
OrderIntent(stop_pct=<above>, trail_pct=<unchanged>)
  │
  ▼
execute_plan → broker.submit_bracket(stop_loss_pct=stop_pct, trailing_stop_pct=trail_pct)
```

Aggressive tranche short-circuits in `_effective_stop_pct` and returns the existing fixed percentage without touching `data` or `indicators`.

---

## 4. Components

### 4.1 `indicators.py` (new)

Pure-compute module. No I/O. No yfinance dependency.

```python
"""Technical indicators. Pure compute — callers fetch the data."""
from typing import Optional
import pandas as pd

def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> Optional[float]:
    """Wilder-smoothed ATR. Returns the most-recent ATR value, or None.

    Returns None when there is insufficient data (fewer than period+1 bars
    after NaN drop) or when inputs are misaligned.
    """
```

**Algorithm**:
- True Range: `TR[t] = max(high[t] - low[t], |high[t] - close[t-1]|, |low[t] - close[t-1]|)`
- Initial ATR: `ATR[period] = mean(TR[1..period])`
- Wilder smoothing: `ATR[t] = (ATR[t-1] × (period-1) + TR[t]) / period`
- Returns the final ATR.

**Edge cases**:
- Fewer than `period + 1` aligned bars → `None`.
- All-NaN series → `None`.
- Constant prices (TR = 0 throughout) → `0.0` (caller treats as degenerate and falls back).

### 4.2 `orders._effective_stop_pct` (new helper in `orders.py`)

```python
def _effective_stop_pct(symbol: str, tranche: str) -> float:
    """Per-symbol initial stop. ATR-scaled for core; fixed for aggressive."""
    base, _ = _tranche_stops(tranche)
    if tranche != "core":
        return base
    try:
        import data, indicators
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

### 4.3 `orders.reconcile_to_targets` (modified)

The buy-intent construction block (`orders.py:248-254`) currently uses the tranche-wide `stop_pct` from `_tranche_stops`. Replace that single value with a per-symbol call:

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

`trail_pct` continues to come from `_tranche_stops` and is unchanged. Sells do not carry a stop and are not affected.

### 4.4 `config.py` additions

```python
# ── Stop-loss ATR scaling (core tranche only) ───────────────────
# Initial stop = min(STOP_LOSS_PCT, ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close).
# Aggressive tranche keeps the fixed AGGRESSIVE_PARAMS["stop_loss_pct"].
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
```

Quant-override allowlist (`config.py` ~line 297):

```python
"ATR_STOP_MULTIPLIER":  (float, 1.0, 4.0),
# ATR_PERIOD: not overrideable — 14 is the standard, keep one fewer knob.
```

The `STOP_LOSS_PCT` entry stays (`(float, 0.04, 0.20)`), but its comment is updated to reflect its new role as an **upper bound** on the per-symbol stop. Same for the equivalent line in `quant/trigger_prompt.md`.

---

## 5. Data flow

1. Rebalancer calls `orders.reconcile_to_targets(targets, tranche="core", ...)`.
2. For each buy diff, `_effective_stop_pct(sym, "core")` is invoked.
3. `data.fetch_ohlcv([sym], "1y")` — 4-hour cache, **keyed by ticker-list hash**, so single-symbol calls do **not** share the screener's bulk-fetch cache. First call per symbol per 4-hour window downloads from yfinance (~1–2 s); subsequent calls hit the cache.
4. `indicators.atr(...)` returns the latest ATR value (float, in dollars).
5. `stop_pct = min(STOP_LOSS_PCT, 2 × ATR / last_close)`. Aggressive path skips steps 3–5 entirely.
6. `OrderIntent.stop_pct` carries the result to `execute_plan` → `broker.submit_bracket` → Alpaca.
7. `portfolio.json` already serializes `stop_pct` per position (`orders.py:464`), so the effective stop is observable post-fill.

**Performance note**: for a typical core rebalance with 5–10 buy intents, the worst case is 5–10 sequential yfinance downloads added to `reconcile_to_targets`, totaling ~10–20 s on a cold cache. Acceptable for a once-per-day cron. If this becomes a problem, the natural refinement is a bulk `fetch_ohlcv(all_buy_symbols, "1y")` at the top of `reconcile_to_targets` followed by per-symbol slicing — kept as a future optimization, not in scope here.

---

## 6. Edge cases & failure modes

| Condition | Behavior | Rationale |
|---|---|---|
| Symbol with <15 OHLC bars (new IPO, ticker change) | Fall back to `STOP_LOSS_PCT` | ATR undefined |
| `yfinance` fetch raises | Fall back | Don't fail a rebalance over data-source flakiness |
| ATR returns `0.0` (constant prices) | Fall back | `2 × 0 / price = 0` would mean no stop |
| `last_close ≤ 0` | Fall back | Defensive against bad data |
| Aggressive tranche | Returns fixed pct immediately | No `data` / `indicators` calls; zero overhead |
| Sell intents | Unaffected | Sells don't carry a stop |
| Unknown tranche | Returns `_tranche_stops` base | Inherits existing semantics |

Every fallback is silent in the rebalance plan output. The printed plan line (`stop=...`) already shows the resolved stop, so divergence between symbols is visible to the operator.

---

## 7. Watchdog interaction (deliberate non-change)

`watchdog.check_prices` (`watchdog.py:115-158`) computes its `STOP-LOSS TRIGGERED` and `Approaching stop-loss` alerts against `STOP_LOSS_PCT` directly. After this change, the broker's actual stop on a low-volatility symbol could be much tighter (e.g. 2%) while watchdog still warns at the 5%/8% balanced thresholds.

This mismatch is **intentional and accepted**:

- The broker-side bracket order is the authoritative protective mechanism. It triggers at the exchange without dependency on `watchdog.py` running.
- Watchdog alerts are belt-and-suspenders for the operator; missing an early warning does not change the executed outcome.
- Keeping watchdog symbol-agnostic avoids adding a per-symbol metadata lookup in the alert path.

A future follow-up could read each position's `stop_pct` from `portfolio.json` and use it as the warning threshold. That work is out of scope here.

---

## 8. Testing

### 8.1 `tests/test_indicators.py` (new)
- `test_atr_known_input` — hand-computed ATR for a small synthetic OHLC table; assert function output matches to 1e-6.
- `test_atr_insufficient_data` — series of length ≤ 14 → `None`.
- `test_atr_constant_price` — high == low == close throughout → `0.0`.
- `test_atr_handles_nan` — sparse NaNs in inputs → still produces a value when ≥ 15 non-NaN bars remain.

### 8.2 `tests/test_orders.py` (extended)
- `test_effective_stop_pct_uses_atr_when_tighter` — mock `data.fetch_ohlcv` to return low-volatility data; assert returned pct < `STOP_LOSS_PCT`.
- `test_effective_stop_pct_caps_at_base` — mock high-volatility data (ATR pct > 8%); assert returned pct == `STOP_LOSS_PCT`.
- `test_effective_stop_pct_aggressive_unchanged` — `tranche="aggressive"`; assert returned pct == `AGGRESSIVE_PARAMS["stop_loss_pct"]` and `data.fetch_ohlcv` is **not** called.
- `test_effective_stop_pct_fallback_on_fetch_error` — `fetch_ohlcv` raises; assert returned pct == base and no exception propagates.
- `test_effective_stop_pct_fallback_on_no_data` — empty / too-short OHLCV; assert fallback.
- `test_reconcile_buy_intent_carries_effective_stop` — end-to-end through `reconcile_to_targets` with mocked ATR; assert `OrderIntent.stop_pct` reflects the ATR-scaled value.
- `test_reconcile_aggressive_buy_intent_unchanged` — same flow for aggressive; assert fixed pct preserved.

### 8.3 Out of scope
No new `tests/test_broker.py` work — `submit_bracket` doesn't care whether the percentage came from ATR or a fixed config. No new integration test required.

---

## 9. Out of scope (recap)

- Aggressive-tranche stop logic (fixed 10% retained).
- Trailing stop (fixed 12%/15% retained).
- Watchdog symbol-aware warning thresholds.
- ATR-based trailing stops ("Chandelier exit").
- Migration of `screener._adr` to `indicators.py`.
- Stop recomputation between entry and exit.

---

## 10. Open questions

None at spec time. Three earlier ambiguities — min-vs-max semantics, scope (core only), and trailing-stop treatment (unchanged) — were resolved in brainstorming.
