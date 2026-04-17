# Intraday Execution Layer — Design

**Date:** 2026-04-17
**Status:** Spec, awaiting implementation plan
**Scope:** Replace one-shot rebalance submission with a planner + cron-driven executor that spreads orders across the trading day, enforces per-order max-price limits, and aborts unexecuted work when market stress signals fire.

---

## 1. Motivation

The current system (`rebalancer.py` → `orders.execute_plan` → `broker.submit_market`) submits every rebalance order as a market order in a single burst at 9:00 AM ET. This has two problems now that we are trading real money through Alpaca:

1. **Execution quality.** Market orders at the open auction accept whatever price Alpaca routes. A $25K rebalance day at 10–30 bps of avoidable slippage is $25–$75 of daily drag. On a weekly/monthly cadence this accumulates.
2. **Stale decisions during the day.** A plan generated at 9:00 AM is locked in — if the Fed makes a surprise announcement at 10:30 AM, orders that had not yet fired would still go out under the original thesis. Nothing in the system can abort an in-flight plan based on new information.

The fix is an **intraday execution layer**: the planner produces a priced, confidence-annotated plan; a separate executor submits slices across the day, skips slices when price exceeds per-order limits, and aborts the remaining plan if a small set of explicit circuit breakers trip.

Explicitly **not** building: continuous intraday *decision* re-evaluation. The underlying signals (dual momentum over 1–12 months, FRED macro series, quarterly fundamentals) have no intraday content. Re-running them every tick adds noise and cost without producing new information. The design treats each day's plan as a batch decision and optimizes only its execution.

---

## 2. Goals & non-goals

### Goals

- Cap per-order slippage using marketable-limit orders with a plan-time `max_price` ceiling.
- Spread larger orders across the trading day (2 or 4 slices depending on tier) to reduce single-tick market impact.
- Abort unexecuted portions of a plan when any of five circuit breakers trip: SPY drop, VIX spike, single-name shock, news shock, macro regime flip.
- Preserve every existing safety rail (HALT, paper/live guard, daily caps, large-order gate) — nothing bypasses `orders.py`.
- Preserve the "everything is reproducible from state on disk + a re-run" property of the current system — no long-running daemon, no event bus.

### Non-goals (v1)

- VWAP/volume-weighted slicing. Even-time slicing first; VWAP if we have evidence it helps.
- Adaptive limit ladders (start at mid, step toward ask on failure to fill). One marketable limit per tick is sufficient for our size.
- Multi-day plan carry-over. Unfilled intents drop at 15:45 ET; tomorrow's rebalancer re-decides.
- Intraday re-decision of strategy signals. Plans are batch decisions.
- A Trump-specific social-media feed. The news-shock breaker (D) subsumes this via generalized keyword + price corroboration.
- Pre-market / after-hours execution. Regular hours only.
- Stop-limit replacement for existing stop-market protections. Keep server-side stop-market at Alpaca.

---

## 3. Architecture

Two cron-driven processes, one shared state file.

```
rebalancer.py  (cadence-gated, 09:35 ET)
      │
      │  builds OrderPlan with confidence + max_price
      │  snapshots SPY / VIX / macro / news baseline
      ▼
.cache/pending_plan.json  ◄───── read/write ─────┐
                                                 │
executor.py  (every 10 min, 10:00–15:50 ET)      │
      │                                          │
      │ 1. evaluate 5 circuit breakers           │
      │ 2. decide per-intent slice action        │
      │ 3. cancel prior unfilled limits          │
      │ 4. submit new slices via orders.py       │
      │    (HALT / caps / large-order all apply) │
      │                                          │
      └──────────────────────────────────────────┘
                │
                ▼
         broker.py → Alpaca
```

### Key invariants preserved from current system

- **Every order funnels through `orders.py`.** `submit_limit_slice` is the new entry point; it runs the same four safety checks as `execute_plan`.
- **Alpaca is the source of truth** for fills, cash, and positions. Local state (`pending_plan.json`) is a workflow artifact.
- **Stateless per tick.** Executor reads disk, decides, submits, writes disk, exits. Crash mid-tick is safe: next tick re-reads state.
- **Idempotent re-runs.** Re-running the planner on the same day overwrites the plan, same as today. Client-order-ids are deterministic per (tranche, reason, symbol, day, slice#), so Alpaca rejects duplicates.

---

## 4. Data structures

### `OrderIntent` (extended in `orders.py`)

```python
@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    notional: float              # total target, not per-slice
    side: str                    # "buy" | "sell"
    reason: str
    tranche: str
    client_order_id: str
    stop_pct: Optional[float]    # existing, on entries
    trail_pct: Optional[float]   # existing, on entries
    # NEW:
    tier: str                    # "HIGH" | "MED"
    decision_price: float        # last trade at plan time
    max_price: float             # buys: decision * (1 + tol); sells: decision * (1 - tol)
    slice_count: int             # 1 | 2 | 4, determined at plan time
```

### `PendingPlan` (new, persisted as `.cache/pending_plan.json`)

```python
{
  "plan_id": "core-2026-04-17",
  "tranche": "core",
  "created_at": "2026-04-17T13:35:00Z",
  "baseline": {
    "spy": 478.22,
    "vix": 14.1,
    "macro_score": 0.12,
    "news_cursor_at": "2026-04-17T13:35:00Z"
  },
  "intents": [
    {
      "intent": { ... OrderIntent ... },
      "status": "active",          # active | aborted | deferred | done
      "notional_filled": 0.0,
      "slices_submitted": 0,
      "last_client_order_id": null,
      "last_limit_price": null,
      "abort_reason": null
    },
    ...
  ],
  "breakers_tripped": []           # ["A", "E"] — sticky for the day
}
```

### New files

| File | Purpose |
|---|---|
| `executor.py` | Cron-driven tick handler. Reads pending plan, evaluates breakers, submits slices, writes updated plan. |
| `news_shock.py` | Keyword-matching news fetcher. Extends `sentiment.py` patterns; exposes `check_news_shock(baseline_cursor, plan_symbols) -> NewsShockResult`. |
| `.cache/pending_plan.json` | Active plan state. Absence = nothing to execute. |
| `.cache/news_shock_log.csv` | Every keyword hit (tripped or not), for threshold tuning. |

### Modified files

| File | Change |
|---|---|
| `broker.py` | Add `submit_limit(symbol, notional/qty, side, limit_price, time_in_force, client_order_id)`. |
| `orders.py` | Extend `OrderIntent`. Add `submit_limit_slice(intent, limit_price, notional, broker)`. Split `execute_plan` into `write_plan` (new, persists pending_plan.json) and keep `execute_plan` as the direct-submit fast path for tiny orders. |
| `rebalancer.py` | After `reconcile_to_targets`, compute confidence → tier → max_price → slice_count per intent; capture baseline; call `write_plan` instead of `execute_plan` for orders above the direct-submit threshold. |
| `watchdog.py` | `submit_exit` writes a HIGH-tier intent into pending_plan.json instead of submitting directly. |
| `config.py` | New `CIRCUIT_BREAKERS`, `EXECUTION_TIERS`, `NEWS_SHOCK_KEYWORDS`, executor window, direct-submit threshold. Bump `DAILY_MAX_ORDERS` 20 → 40. |
| `momentum.py`, `screener.py` | Expose rank/confidence on each selection so the planner can map to tier. |
| `README.md` | Document new execution flow, cron schedule, rollout phases. |

---

## 5. Execution logic

### Confidence tier

Two tiers. No continuous score — underlying signals don't support that precision.

| Tier | Who qualifies |
|---|---|
| **HIGH** | Top-1 ETF in dual momentum, top-1 stock in screener, defensive allocations (BIL/SHY/IEF), macro-driven exits from watchdog |
| **MED** | All other rebalancer picks (top-2..N) |

Each signal module returns a ranked list; the planner assigns HIGH to rank-1 and MED to the rest. Macro exits are always HIGH.

### Tolerance → max_price

Tolerance (basis points against `decision_price`):

| Tier | ETF | Stock |
|---|---|---|
| HIGH | 50 | 100 |
| MED | 30 | 50 |

Aggressive tranche (leveraged ETFs): multiply by 1.5.
Macro exits: 150 bps (HIGH tier, but override the default HIGH tolerance — the goal is out, not optimization).

At plan time:
- Buys: `max_price = decision_price * (1 + tolerance)`
- Sells: `min_price = decision_price * (1 - tolerance)` (stored in the same `max_price` field; sign-aware at use site)

### Slice count

| Notional | Slice count |
|---|---|
| < $500 | 1 (planner submits directly, bypasses executor) |
| $500 – $2,000 | 2 |
| ≥ $2,000 and HIGH | 2 |
| ≥ $2,000 and MED | 4 |

Slice windows are pinned to 10-min tick boundaries across 10:00–15:00 ET (31 ticks). A 4-slice plan targets ticks 10:00, 11:40, 13:20, 15:00. A 2-slice plan targets 10:30 and 14:30. The 15:10–15:50 ticks are used for cleanup (cancel unfilled, defer).

### Per-tick algorithm (executor)

For each tick during market hours 10:00–15:50 ET:

1. Load `pending_plan.json`. If absent or all intents terminal → exit.
2. If HALT file present → log and exit.
3. If market closed (per Alpaca clock) → log and exit.
4. Fetch current SPY, VIX, macro score (cached hourly), news feed since `news_cursor_at`, per-plan-symbol last trades.
5. Evaluate each circuit breaker. If tripped and not already in `breakers_tripped`: add to the list, abort intents per the breaker's scope, emit Telegram notification, log to `daily_log.csv`.
6. For each `active` intent not already aborted this tick:
   a. Cancel any outstanding limit from prior tick (stored in `last_client_order_id`) whose fill status is not complete.
   b. Update `notional_filled` from the prior order's fill status (Alpaca is source of truth).
   c. Decide: is this tick's timestamp at or past the next scheduled slice window? If no, continue.
   d. Compute slice size: `(notional_target - notional_filled) / (slice_count - slices_submitted)`.
   e. Compute limit price: buys `min(current_ask * 1.001, max_price)`; sells `max(current_bid * 0.999, min_price)`.
   f. If the computed limit crosses the wrong side of `max_price`/`min_price` (i.e., would violate the ceiling/floor) → skip this slice, **do not cancel it**, re-try next tick.
   g. Otherwise, call `orders.submit_limit_slice(intent, limit_price, slice_size, broker)`. Store the returned `client_order_id` as `last_client_order_id`. Increment `slices_submitted`.
7. At the 15:50 ET tick (last of the day): cancel every outstanding limit. Any intent with `notional_filled < notional_target * 0.95` is marked `deferred`. `pending_plan.json` is retained for audit, cleared at next rebalancer run.
8. Write `pending_plan.json`.

---

## 6. Circuit breakers

All thresholds live in `config.CIRCUIT_BREAKERS` and are tunable.

| # | Breaker | Measurement | Default threshold | Data source | Scope when tripped |
|---|---|---|---|---|---|
| A | **SPY drop** | `(spy_now − spy_baseline) / spy_baseline` | ≤ −1.5% | Alpaca last trade | Abort all buys; sells continue |
| B | **VIX spike** | `vix_now` vs baseline | `vix_now > max(baseline × 1.5, 25.0)` | yfinance `^VIX` | Abort all buys |
| C | **Single-name shock** | per-symbol change from baseline | ≤ −5% | Alpaca last trade | Abort **only that symbol's** intent |
| D | **News shock** | keyword hit in last 30 min **AND** SPY moved >0.5% in the same 15-min window | both conditions true | Yahoo Finance news RSS + Reddit hot (`news_shock.py`, extends `sentiment.py`) | Abort all buys |
| E | **Macro regime flip** | macro score vs baseline | score dropped ≥ 0.3 | `macro.py`, refresh hourly | Abort **risk-on buys only**; defensive buys continue (see below) |

### Rules common to all breakers

- **Sticky for the day.** Once tripped, the affected scope stays aborted for the rest of the trading day. VIX returning to baseline does not re-enable aborted intents. Tomorrow's rebalancer re-plans with a fresh baseline.
- **Aborted intents drop from the plan** identically to 15:50 unfilled intents — tomorrow's rebalancer sees the gap and re-decides based on the day's signals.
- **Telegram notification** on trip, one per (breaker, plan) pair. Includes measured value, threshold, and list of aborted symbols.
- **Infrastructure errors are not breakers.** Alpaca rate-limit, FRED outage, yfinance failure → log, skip tick, retry next tick. Never abort on our own failure.

### Breaker D implementation notes

- **Keywords** (config, default): `tariff`, `tariffs`, `sanctions`, `rate cut`, `rate hike`, `fed`, `powell`, `fomc`, `war`, `military`, `invasion`, `shutdown`, `default`, `recession`. Plus the ticker symbols in the active plan.
- **Dedupe window:** 60 min on title-hash. Prevents RSS re-listings or Reddit cross-posts from generating repeat trips.
- **Every keyword hit is logged** to `.cache/news_shock_log.csv` regardless of whether it tripped, so thresholds can be tuned from real data in Phase 1.
- **Corroboration is mandatory.** Keyword hit without a coincident >0.5% SPY move does not trip. This rejects non-market-moving political content.

### Defining "risk-on" vs "defensive" for breaker E

A symbol is **defensive** if it appears in `DEFENSIVE_SYMBOLS = {"BIL", "SHY", "IEF", "TLT"}` (config, tunable). All other symbols are **risk-on**. Under breaker E, only risk-on intents are aborted; defensive buys proceed (rotating into defensives is the correct response to a macro deterioration).

---

## 7. Exits & protective stops

- **Rebalance sells** — executor-driven, same tiering and slicing as buys, with `min_price` floor instead of `max_price` ceiling.
- **Macro-driven exits** via `watchdog.submit_exit` — HIGH tier, 150 bps tolerance, 2 slices. Worst-case latency from watchdog detection to first slice is ~10 min (next executor tick). Acceptable.
- **Stop-losses and trailing stops** — remain **server-side at Alpaca**. `broker.submit_bracket` is retained for existing paths but unused on executor-driven entries; instead, `ensure_trailing_stops` is called at the 15:50 tick on any intent that reached ≥95% of `notional_target`, and on partially-filled intents for the filled portion.
- **Stop-market, not stop-limit.** Decision: stop-limit introduces "triggered but never fills" risk on gap-downs, which is worse than a market-order bad fill. Keep stop-market.

---

## 8. Safety-rail integration

All four existing rails apply without modification, enforced inside `orders.submit_limit_slice`:

1. **HALT** — executor checks `.cache/HALT` at the start of every tick; if present, exits without action. Per-intent submissions also re-check inside `orders.py` for belt-and-braces.
2. **Paper/live guard** — unchanged, in `broker.py` constructor.
3. **Daily caps** — counted per **slice submitted**, not per intent. A 4-slice $8K intent contributes 4 to `DAILY_MAX_ORDERS`. `DAILY_MAX_ORDERS` is therefore bumped from 20 → 40 in `config.py`; `DAILY_MAX_NOTIONAL` is unchanged ($25K is already the budget). Slices that hit the cap are deferred, same as today.
4. **Large-order gate** — triggers per slice. A single slice ≥ `LARGE_ORDER_THRESHOLD` ($2K default) is queued to `pending_orders.json` for Telegram approval. Approved slices resume on the next tick; rejected slices cause the intent to be marked `aborted` with `abort_reason="rejected by user"`.

---

## 9. Cron schedule

```bash
# Watchdog — unchanged
30 8 * * 1-5  cd /Users/zl/works/stock && python3 watchdog.py                         >> .cache/watchdog.log 2>&1

# Rebalancer — 09:00 → 09:35 ET (post-open so SPY/VIX baselines are live)
35 9 * * 1-5  cd /Users/zl/works/stock && python3 rebalancer.py --tranche core        >> .cache/rebalance.log 2>&1
35 9 * * 1    cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive  >> .cache/rebalance.log 2>&1

# NEW: executor — every 10 min, 10:00–15:50 ET
*/10 10-15 * * 1-5  cd /Users/zl/works/stock && python3 executor.py                   >> .cache/executor.log 2>&1
```

Executor no-ops if `pending_plan.json` is empty — safe on non-rebalance days.

---

## 10. Configuration additions

```python
# config.py additions

EXECUTOR_WINDOW_START = "10:00"              # ET
EXECUTOR_WINDOW_END   = "15:50"              # ET
EXECUTOR_TICK_MINUTES = 10
PLANNER_DIRECT_SUBMIT_THRESHOLD = 500.0      # USD — below this, planner submits directly

EXECUTION_TIERS = {
    "HIGH": {"etf_bps": 50, "stock_bps": 100},
    "MED":  {"etf_bps": 30, "stock_bps": 50},
}
AGGRESSIVE_TIER_MULTIPLIER = 1.5
MACRO_EXIT_TOLERANCE_BPS = 150               # overrides HIGH default for macro exits

# Slice count by (tier, notional bucket). "small" = $500–$2000, "large" = ≥$2000.
SLICE_COUNTS = {
    "HIGH": {"small": 2, "large": 2},
    "MED":  {"small": 2, "large": 4},
}
SLICE_SIZE_SMALL_MAX = 2000.0                # above this notional, use "large" slice count
DEFENSIVE_SYMBOLS = {"BIL", "SHY", "IEF", "TLT"}   # breaker E exempts these from abort

CIRCUIT_BREAKERS = {
    "spy_drop_pct":             0.015,
    "vix_multiplier":           1.5,
    "vix_absolute":             25.0,
    "single_name_drop_pct":     0.05,
    "news_corroboration_pct":   0.005,
    "news_window_minutes":      15,
    "news_dedupe_minutes":      60,
    "macro_drop":               0.3,
}

NEWS_SHOCK_KEYWORDS = [
    "tariff", "tariffs", "sanctions",
    "rate cut", "rate hike", "fed", "powell", "fomc",
    "war", "military", "invasion",
    "shutdown", "default", "recession",
]

# Daily caps bump
DAILY_MAX_ORDERS = 40    # was 20 — slice-per-tick counts

# Phase 0 shadow flag
EXECUTOR_SHADOW_MODE = True    # logs intended submissions without placing them
```

---

## 11. Testing

Extends existing `tests/fakes.py` and pytest structure.

### New fakes

- `FakeMarketData` — deterministic SPY/VIX/per-symbol prices indexed by fake clock.
- `FakeNewsFeed` — canned keyword hits at specific timestamps with title hashes.
- `FakeClock` (extension of existing) — supports advancing in 10-min ticks across a simulated trading day.

### Unit test coverage

- Each circuit breaker: trips at threshold, does not trip just below, sticky-for-day, correct scope.
- Slicing: 1/2/4-slice math correct; respects partial fills; cancels-before-resubmits.
- `max_price` gating: slice skipped but not canceled when ask exceeds ceiling; re-tries next tick; drops at 15:50.
- Defer-to-tomorrow: 15:50 cutoff marks intents `deferred`; next day's rebalancer re-plans correctly against live positions.
- Safety rails: HALT blocks whole tick; daily cap blocks per-slice; large-order gate queues per-slice; Telegram rejection aborts intent.
- Macro exit: HIGH-tier, 150 bps tolerance, 2 slices, survives end-to-end.
- News shock: keyword alone does not trip, keyword + 0.5% SPY move trips, 60-min dedupe works.
- Shadow mode: executor logs intent but no submissions reach the fake broker.

### Integration test (opt-in, paper-only)

Small $500 plan submitted to Alpaca paper. Run 1 simulated day of executor ticks against real Alpaca paper data. Verify fills, cancellations, and final state match the local state file.

---

## 12. Rollout

Three sequential phases. Do not skip.

1. **Phase 0 — shadow (1–2 weeks, paper).** `EXECUTOR_SHADOW_MODE = True`. Executor reads pending plans and logs what it *would* submit, without submitting. Planner continues submitting directly (current behavior). Diff the shadow log against actual rebalancer submissions daily to validate logic.
2. **Phase 1 — live-on-paper (2–4 weeks).** `EXECUTOR_SHADOW_MODE = False`. Planner stops direct submission. Executor takes over all execution on the paper account. Tune circuit breaker thresholds from real trips (expect 0–2 legitimate trips in this window). Monitor `daily_log.csv` and Alpaca dashboard.
3. **Phase 2 — live.** Standard paper→live protocol per existing README: `ALPACA_ENV=live`, `ALPACA_LIVE_CONFIRM=yes`, `DAILY_MAX_NOTIONAL` initially small, ramped over subsequent weeks.

---

## 13. Open questions

None remaining at spec time. The following were considered and explicitly closed:

- Continuous confidence score → closed in favor of two discrete tiers.
- Daemon process for the executor → closed in favor of cron-driven stateless ticks.
- Stop-limit replacing stop-market → closed in favor of keeping stop-market.
- Trump-specific feed → closed in favor of generalized news-keyword detector with price corroboration.
- Intraday strategy re-decision → closed as out-of-scope; underlying signals have no intraday content.
