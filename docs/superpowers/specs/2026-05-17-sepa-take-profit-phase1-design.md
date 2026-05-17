# SEPA Take-Profit (Phase 1) — Design

**Date:** 2026-05-17
**Status:** Spec, awaiting implementation plan
**Scope:** Add Mark Minervini SEPA-style take-profit logic to the core tranche. Phase 1 covers two rules: R-multiple scale-out (sell 1/3 at 2R, 1/3 at 3R) and 21EMA trailing exit on the remaining 1/3. Phase 2 (climax detection, failed-breakout 3-day rule) is out of scope and will be a separate spec.

---

## 1. Motivation

The system today has no explicit take-profit logic. Winners are held until either a fixed stop-loss or a fixed-percentage trailing stop triggers. Two consequences:

- **No partial profit-taking.** A position that runs 50% in your favor can give it all back through one bad day before the trailing stop catches up.
- **Trailing stop is symbol-agnostic.** A 12% trail on a low-volatility ETF is too loose; on a volatile stock it's too tight.

Mark Minervini's SEPA framework prescribes a scale-out: sell partial size at predefined multiples of initial risk (2R, 3R), then trail the remainder with a moving average that reflects the trend. This Phase 1 implements that for the core tranche.

The aggressive tranche (leveraged ETFs) is **excluded** — Minervini's stage analysis assumes single-name uptrends, not leveraged-ETF rotation.

---

## 2. Goals & non-goals

### Goals
- Per-position R-multiple scale-out at 2R (sell 1/3 of initial qty) and 3R (sell another 1/3 of initial qty).
- 21EMA trailing exit on the remaining 1/3 (close below 21EMA → full exit).
- Trailing-stop coordination: after each partial sell, the broker-side trailing-stop is re-issued for the new (reduced) qty. After the 3R partial sell, the trailing-stop is cancelled entirely — the remaining 1/3 is protected only by the 21EMA rule and the original bracket stop-loss.
- Idempotency: re-running the watchdog never resubmits the same partial sell.
- Telegram notification on each scale-out and full exit.
- Core tranche only.

### Non-goals
- Climax run / blow-off detection (Phase 2).
- Failed-breakout 3-day rule (Phase 2).
- Stage-3 transition exit (Phase 2 or later).
- Aggressive-tranche adaptation.
- Quant-review subagent overrides for SEPA parameters (kept static in v1).
- Recomputation of `initial_stop_price` after the user manually edits `stop_pct` — initial fields are immutable once snapshotted.
- Intraday SEPA checks — Phase 1 runs once per day in watchdog (mirrors Minervini's daily-close decision cadence).

---

## 3. Architecture

```
sync_state(broker)
  ├─ snapshots immutable initial_entry_price / initial_qty / initial_stop_price
  │  on first sight of a core position
  └─ when current_qty drops below an R-tier threshold, appends "2R" / "3R" to r_tier_filled
       │
       ▼
watchdog.check_sepa_exits(snap, broker)
  for each position in snap.by_tranche("core"):
    │
    ├─ sepa_exits.next_r_tier_action(position, last_price)
    │     ├─ "2R" → orders.submit_partial_exit(sym, 1/3, "sepa-2R")
    │     │        + orders.cancel_position_trailing(sym)
    │     │        + broker.submit_trailing_stop(sym, qty=initial_qty*2/3, ...)
    │     │        + telegram notify
    │     │
    │     └─ "3R" → orders.submit_partial_exit(sym, 1/3, "sepa-3R")
    │              + orders.cancel_position_trailing(sym)
    │              + telegram notify  (no re-trail; final 1/3 is MA-protected)
    │
    └─ if "3R" in r_tier_filled:
         sepa_exits.ma_trail_should_exit(position, closes)
            └─ True → orders.submit_exit(sym, "sepa-21EMA-break") + telegram
```

All side-effects route through existing `orders` / `broker` primitives. SEPA itself is two layers: a pure-compute decision module (`sepa_exits.py`) and an orchestration function in `watchdog.py`.

---

## 4. Components

### 4.1 `portfolio.json` schema extension

Per-position fields added by `orders.sync_state`:

```jsonc
{
  "symbol": "AAPL",
  "shares": 30,
  "avg_entry": 100.0,                  // existing — updates if you add to position
  "stop_pct": 0.08,                    // existing
  "trail_pct": 0.12,                   // existing
  "market_value": 3500.0,              // existing
  "tranche": "core",                   // existing

  // NEW — immutable after first sync_state encounter:
  "initial_entry_price": 100.0,
  "initial_qty": 30,
  "initial_stop_price": 92.0,          // = avg_entry × (1 − stop_pct) at first sight
                                       // null if stop_pct unknown → SEPA skips

  // NEW — append-only by sync_state when observed qty drops:
  "r_tier_filled": []                  // ["2R"] or ["2R", "3R"]
}
```

**Initialization rules in `sync_state`:**
- New position first seen with `tranche="core"` and known `stop_pct`:
  - `initial_entry_price = avg_entry`
  - `initial_qty = current_qty`
  - `initial_stop_price = avg_entry × (1 − stop_pct)`
  - `r_tier_filled = []`
- New position with unknown `stop_pct` (external / pre-SEPA legacy):
  - `initial_stop_price = None` → SEPA permanently skips this position
  - Other initial fields still snapshotted for visibility
- Subsequent runs: if `initial_*` already set, **never touched again** (immutability).
- `r_tier_filled` mutation: for each tier `(R, f)` in `config.SEPA_R_TIERS` order, if its label is not yet in `r_tier_filled`:
  - `cumulative_fraction = sum(frac for (_, frac) in SEPA_R_TIERS[:i+1])` — fractions through this tier inclusive
  - `threshold = initial_qty × (1 − cumulative_fraction) + ε` (ε = 1 share, for fractional-share rounding tolerance)
  - if `current_qty ≤ threshold`, append the tier's label and continue the loop (handles the gap-up case where two tiers fill on the same day)
  - else, break out of the loop (subsequent tiers haven't completed either)

  Example: `initial_qty = 30`, `r_tier_filled = []`, `current_qty = 10` → loops through "2R" (threshold 21, qty ≤ → append) then "3R" (threshold 11, qty ≤ → append). Resulting `r_tier_filled = ["2R", "3R"]`.

### 4.2 `sepa_exits.py` (new module, pure compute)

```python
"""Mark Minervini SEPA exit rules — Phase 1 (R-multiple scale-out + 21EMA trail).

All functions are pure: side-effect free, no I/O, no broker access. Callers
fetch the data and feed it in.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

import config


def initial_r(position: dict) -> Optional[float]:
    """R per share = initial_entry_price − initial_stop_price.

    Returns None if either initial field is missing (e.g. unknown-tranche
    position carried forward without an entry stop).
    """


def r_multiple(position: dict, current_price: float) -> Optional[float]:
    """(current_price − initial_entry_price) / R.

    Returns None when R is unknown or zero.
    """


def next_r_tier_action(position: dict, current_price: float) -> Optional[str]:
    """Return the next R-tier label to action, or None.

    Iterates config.SEPA_R_TIERS in order; returns the first tier whose
    R-multiple is reached AND whose label is not already in r_tier_filled.
    Returns None if no tier qualifies or the position lacks an initial stop.
    """


def ma_break(closes: pd.Series, period: int = 21, ma_type: str = "ema") -> Optional[bool]:
    """True if the most recent close < EMA(period). None on insufficient data.

    `ma_type` "ema" uses pandas' .ewm(span=period); "sma" uses rolling mean.
    """


def ma_trail_should_exit(position: dict, closes: pd.Series) -> bool:
    """True only when r_tier_filled contains the final tier label AND ma_break is True.

    "Final tier" = the last label in config.SEPA_R_TIERS (currently "3R").
    Returns False (not None) when gating conditions aren't met — this is the
    "do nothing" signal, distinct from data-unavailable.
    """
```

Tier labels are derived once at import time: `f"{int(r)}R"` from each `(r, _)` in `SEPA_R_TIERS`. This keeps the labels and thresholds in lockstep.

### 4.3 `orders.submit_partial_exit` (new)

```python
def submit_partial_exit(symbol: str, *, fraction_of_initial: float,
                        reason: str, broker) -> ExecutionResult:
    """Partial sell. Notional = initial_qty × fraction_of_initial × current_price.

    Writes a HIGH-tier intent to pending_plan.json with 150 bps tolerance
    (same path as submit_exit). Executor.py picks it up on the next 10-min
    tick and slices it out.

    Conflict resolution mirrors submit_exit: if pending_plan.json already
    contains an intent for `symbol`, falls back to direct execute_plan so
    we don't double-target. Records the partial in DAILY_TRADE_LOG.
    """
```

Reads `initial_qty` and `current_price` from the portfolio cache + broker. If `initial_qty` is missing (legacy position), records a skipped intent and returns; SEPA is no-op for that symbol.

### 4.4 `orders.cancel_position_trailing` (new)

```python
def cancel_position_trailing(symbol: str, broker) -> ExecutionResult:
    """Cancel any open trailing_stop orders on symbol. No-op when absent.

    Respects HALT. Bypasses daily-cap and large-order gates (cancellations
    are zero-notional and never queued for approval).
    """
```

Filters `broker.get_open_orders()` for `type == "trailing_stop" and symbol == sym`, then calls `broker.cancel_order(order_id)` per match.

### 4.5 `watchdog.check_sepa_exits` (new)

```python
def check_sepa_exits(snap: orders.PortfolioSnapshot, broker) -> list[str]:
    """SEPA Phase 1 driver. Returns Telegram-bound notification lines.

    For each position in snap.by_tranche("core"):
      1. next_r_tier_action → "2R" → submit_partial + cancel_trailing + re-trail(qty*2/3)
      2. next_r_tier_action → "3R" → submit_partial + cancel_trailing (NO re-trail)
      3. if "3R" in r_tier_filled → ma_trail_should_exit → submit_exit (full)

    No side-effects on positions outside core. No-op when config.SEPA_ENABLED
    is False or the position has initial_stop_price=None.
    """
```

Wired into `watchdog.py:main` after `check_prices` and before `act_on_macro_flip`. The returned notification list is appended to the Telegram queue (existing path: `TELEGRAM_NOTIFY_PATH`).

### 4.6 `config.py` additions

```python
# ── SEPA take-profit (Phase 1: core tranche only) ────────────────
SEPA_ENABLED = True
SEPA_R_TIERS = [(2.0, 1/3), (3.0, 1/3)]   # (R-multiple, fraction-of-initial-qty)
SEPA_MA_PERIOD = 21
SEPA_MA_TYPE = "ema"                       # "ema" | "sma"
SEPA_MA_HISTORY = "6mo"                    # data.fetch_prices period for the EMA
```

No entries added to `_OVERRIDE_SCHEMA` in v1 — SEPA parameters stay static until we've run them for a few weeks.

---

## 5. State machine (per position)

```
[initial entry: r_tier_filled=[], trailing@qty=initial]
  │
  │ watchdog sees price ≥ 2R target
  ▼
submit_partial(1/3 initial qty, reason="sepa-2R")
cancel_position_trailing(sym)
broker.submit_trailing_stop(sym, qty=initial_qty*2/3, ...)
  │
  │ executor slices the sell over the day
  │ next watchdog sees current_qty ≈ 2/3 initial
  ▼
sync_state appends "2R" to r_tier_filled
  │
  │ watchdog sees price ≥ 3R target
  ▼
submit_partial(1/3 initial qty, reason="sepa-3R")
cancel_position_trailing(sym)        # no re-trail
  │
  │ executor slices the sell
  │ next watchdog sees current_qty ≈ 1/3 initial
  ▼
sync_state appends "3R" to r_tier_filled
  │
  │ watchdog: close < 21EMA on this symbol
  ▼
orders.submit_exit(sym, reason="sepa-21EMA-break")
  │
  ▼
[position closed: bracket stop also still active until full exit]
```

**Idempotency invariants:**
- `next_r_tier_action` consults `r_tier_filled` AND existing `pending_plan` intents on the symbol. A re-run of watchdog on the same day after a partial submission returns `None` for that tier.
- `sync_state` only appends to `r_tier_filled` once per qty-drop observation; if the list already contains the label that the qty drop implies, no-op.
- `cancel_position_trailing` and re-trailing are atomic from watchdog's perspective: failure to re-trail is logged as alert but does not roll back the partial sell.

---

## 6. Edge cases

| Condition | Behavior |
|---|---|
| Position price gaps from below 2R to above 3R in one day | Day-N watchdog submits 2R partial. Day-N+1 watchdog observes qty drop, appends "2R", and submits 3R partial. Two-day scale-out by design (idempotency via observed qty). |
| 2R partial submitted but executor defers (HALT / cap / market closed) | Pending intent stays in pending_plan. Next watchdog: pending_plan still has intent → `next_r_tier_action` returns None → no double-submit. When the deferred intent finally clears and qty drops, sync_state appends "2R". |
| Broker stop-loss triggers before 2R | Position closes at broker. Next sync_state drops the closed position. SEPA never runs for it. |
| `initial_stop_price = None` (unknown-tranche or legacy position) | `initial_r` returns None → `next_r_tier_action` returns None → SEPA silently skips. |
| User manually adds to position after entry | `initial_*` fields are immutable — already snapshotted on first sight. SEPA computes R against the original entry. The added shares get the same scale-out treatment proportionally (partial sells use `initial_qty × fraction`, so over-adds lead to over-selling: 30 + 30 = 60 current; 2R partial sells `30 × 1/3 = 10`, leaving 50 of 60). Documented; not auto-corrected in v1. |
| Position growth via rebalancer (not user) | Same as manual add. Future Phase 2/3 may handle DCA semantics; out of scope here. |
| Aggressive-tranche position | `snap.by_tranche("core")` doesn't include it. Full bypass. |
| `r_tier_filled` contains "3R" but trailing-stop is still attached (re-trail failure) | watchdog re-attempts `cancel_position_trailing` each run until it succeeds. The 21EMA exit is independent and still operative. |
| `config.SEPA_ENABLED = False` | `check_sepa_exits` returns `[]` immediately. |
| HALT file present | `submit_partial_exit` and `submit_exit` both check HALT and return early with skipped intents. Telegram notification still queues so operator sees what was suppressed. |
| 21EMA insufficient data (<22 daily closes) | `ma_break` returns None; `ma_trail_should_exit` returns False → no exit attempted. |
| MA-trail data fetch raises | `check_sepa_exits` catches; emits alert; no exit attempted. |

---

## 7. Notifications

Each scale-out / full exit appends one entry to `config.TELEGRAM_NOTIFY_PATH`. Format mirrors the existing executor / rebalancer entries:

```
🎯 SEPA 2R hit — AAPL
Sold 1/3 (10 shares ≈ $1,160) at $116.0
R = $8.0, multiple = 2.0
Re-trailing 20 remaining shares @ 12%
```

```
🎯 SEPA 3R hit — AAPL
Sold 1/3 (10 shares ≈ $1,240) at $124.0
Trailing-stop removed; final 10 shares now MA-trailing 21EMA
```

```
📉 SEPA 21EMA break — AAPL
Last close $107.5 < 21EMA $108.9
Submitting full exit on remaining 10 shares
```

---

## 8. Testing

### 8.1 `tests/test_sepa_exits.py` (new — pure compute)

- `test_initial_r_basic` — entry 100, stop 92 → R = 8
- `test_initial_r_missing_stop_returns_none`
- `test_r_multiple_basic` — at price 116 with R=8 entry 100 → 2.0
- `test_r_multiple_below_entry_negative`
- `test_r_multiple_no_R_returns_none`
- `test_next_r_tier_action_empty_filled_2r_reached` → "2R"
- `test_next_r_tier_action_filled_2r_3r_reached` → "3R"
- `test_next_r_tier_action_all_filled_returns_none`
- `test_next_r_tier_action_below_2r_returns_none`
- `test_ma_break_close_below_ema_true`
- `test_ma_break_close_above_ema_false`
- `test_ma_break_insufficient_data_none`
- `test_ma_trail_should_exit_requires_final_tier_filled`

### 8.2 `tests/test_orders.py` (extend)

- `test_submit_partial_exit_writes_to_pending_plan`
- `test_submit_partial_exit_conflict_falls_back_to_direct`
- `test_submit_partial_exit_skips_when_initial_qty_missing`
- `test_submit_partial_exit_respects_halt`
- `test_cancel_position_trailing_cancels_open_trailing_order`
- `test_cancel_position_trailing_noop_when_no_trailing`
- `test_cancel_position_trailing_respects_halt`

### 8.3 `tests/test_orders.py` — `sync_state` (extend)

- `test_sync_state_snapshots_initial_fields_on_first_seen_core_position`
- `test_sync_state_preserves_initial_fields_across_runs`
- `test_sync_state_initial_stop_none_when_stop_pct_missing`
- `test_sync_state_appends_r_tier_when_qty_drops_to_two_thirds`
- `test_sync_state_appends_r_tier_3R_when_qty_drops_to_one_third`
- `test_sync_state_does_not_append_r_tier_on_full_qty`

### 8.4 `tests/test_watchdog.py` (new or extend)

- `test_check_sepa_exits_2r_path` — FakeBroker, set_latest_price triggers 2R; assert partial + cancel + re-trail
- `test_check_sepa_exits_3r_path` — r_tier_filled=["2R"]; trigger 3R; assert partial + cancel; assert NO re-trail
- `test_check_sepa_exits_ma_break_path` — r_tier_filled=["2R","3R"]; mock 6mo closes such that last < EMA21; assert submit_exit
- `test_check_sepa_exits_skips_aggressive_tranche`
- `test_check_sepa_exits_skips_when_initial_stop_none`
- `test_check_sepa_exits_skips_when_pending_plan_has_conflict`
- `test_check_sepa_exits_disabled_when_config_off`
- `test_check_sepa_exits_telegram_notification_appended`

### 8.5 Out of scope

No new integration test against live Alpaca. Existing `test_integration.py` opt-in suite remains unchanged.

---

## 9. Open questions

None at spec time. Five questions raised during brainstorming — immutable initial fields, append-on-observation semantics, re-trail qty, Telegram notify, MA data source — were all resolved before this spec was written.

---

## 10. Out of scope (recap)

- Climax run / blow-off detection (Phase 2)
- Failed-breakout 3-day rule (Phase 2)
- Stage-3 transition exit rule (Phase 2+)
- Aggressive-tranche SEPA
- Quant-review override allowlist entries for SEPA
- Reconciliation of trailing-stop qty after re-trail failure (best-effort in v1)
- Configurable per-symbol R-tier overrides
- Backfill of `initial_*` fields for legacy positions without `stop_pct` data
