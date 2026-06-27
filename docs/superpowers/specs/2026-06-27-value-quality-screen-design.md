# Design: Multi-strategy ensemble → agent pick → watchdog auto-buy

**Date:** 2026-06-27
**Status:** Approved (design)
**Scope:** Run multiple stock-selection strategies in isolation, each emitting
its own candidate list; have a single Claude agent review all lists and pick the
top 4 potential buys; feed those 4 to the existing watchdog buy path, which makes
the final buy/no-buy timing decision and **auto-executes** (no manual approval),
exactly like the current `check_buy_signals` flow.

## Background

Today's buy flow is: `screener.py` (CANSLIM) → cached screened list →
`watchdog.check_buy_signals` evaluates an intraday volume-breakout timing
condition per candidate → submits the buy. There is only one strategy
(growth/momentum) and no valuation lens. This redesign:

1. Adds a **value+quality** strategy (the "low value, high potential" source).
2. Runs each strategy **isolated**, writing its own result file.
3. Inserts a **single Claude agent** that reviews all lists and selects the
   **top 4**.
4. Points the **existing watchdog buy path** at those 4 — unchanged buy
   mechanism, new candidate source.

## Goals

- Strategies are independent: each is its own module, reads nothing from the
  others, writes its own `.cache/strategies/<name>.json`.
- One agent reviews all strategy lists → top 4 with written rationale.
- Watchdog monitors only those 4 and auto-buys on a confirmed timing signal,
  through the existing order pipeline (HALT / daily caps / large-order rails).

## Non-goals

- No new data provider (uses existing yfinance `fetch_info` / `fetch_prices`).
- No manual buy-approval step (auto-buy by design).
- No change to the rebalancer or the ETF-rotation/momentum sleeve.

## Architecture (3 components + a runner)

```
value_screen.py  ─► .cache/strategies/value.json    ┐ isolated
screener.py      ─► .cache/strategies/canslim.json   ┘ strategy lists
                          │
            investor_agent.select_candidates()  (one Claude agent)
                          │  reviews both lists, dedupe + sanity filter,
                          ▼  picks top 4 with rationale
                 .cache/buy_candidates.json
                          │
          watchdog.check_buy_signals  (reads top-4 instead of raw screener)
                          │  existing timing test → final decision
                          ▼
                 auto-buy via orders pipeline (HALT / caps / approval rails)
```

### Component 1 — `value_screen.py` (new strategy)

Pure value+quality screen producing a ranked list. (Same factor design as the
prior spec, now scoped to *just emit a list* — no standalone timing monitor.)

- Universe: `--tickers` or default `config.WATCHLIST`.
- Per ticker, from `data.fetch_info` (+ optional `fetch_fundamentals`):
  - **Gates** (drop first): liquidity `avg_volume*price ≥ VS_MIN_DOLLAR_VOLUME`;
    `price ≥ VS_MIN_PRICE`; `marketCap ≥ VS_MIN_MARKET_CAP`; trap guard
    `freeCashflow>0 OR returnOnEquity>0`.
  - **Value factors:** fcf_yield, earnings_yield (1/forwardPE), ev_ebitda_inv,
    book/market (1/priceToBook).
  - **Quality factors:** roe, inv_debt = 1/(1+debtToEquity).
  - **Improving (optional):** eps_q_growth, revenue_growth.
- Cross-sectional winsorized z-score per factor, averaged within sub-group, then
  composite = `0.5*value_z + 0.35*quality_z + 0.15*improving_z`.
- Emit top `VS_TOP_N` to `.cache/strategies/value.json` via a shared writer.

### Component 2 — strategy result contract + runner

A shared module `strategies.py` defines the **isolated-strategy contract**:

```python
# A strategy result row:
{ "ticker": str, "score": float, "rank": int, "factors": dict }
# write_strategy_result(name, rows) -> .cache/strategies/<name>.json
#   { "strategy": name, "generated_at": iso, "rows": [...] }
# load_strategy_results() -> {name: parsed_json, ...}  (fail-open per file)
```

- `value_screen.py` and a thin `canslim` adapter (wrapping the existing
  `screener.screen_stocks()` output) both write through `write_strategy_result`.
- A runner `run_strategies()` invokes each registered strategy in isolation
  (one failing strategy never blocks the others) and returns the set of result
  files. Wired into the daily cron before the agent step.

### Component 3 — agent selection (extend `investor_agent.py`)

`investor_agent.select_candidates(top_n=4) -> list[dict]`:

- `load_strategy_results()` → merge rows across strategies, **dedupe by ticker**
  (keep best rank; record which strategies surfaced it).
- **Rule pre-filter** (cheap sanity, not selection): drop tickers failing
  liquidity/price floor or already owned (`sync_state` positions).
- Build a compact prompt: per-strategy lists + the merged/deduped pool with
  cross-strategy agreement flags. Shell to the local `claude` CLI (reusing the
  module's existing non-interactive `-p` pattern) asking for **exactly `top_n`
  picks with a one-line rationale each**, returned as JSON.
- Validate the JSON (tickers must be from the pool; exactly `top_n`); on any LLM
  failure, **fall back** to the rule-ranked top-N (consensus-first, then best
  composite) so the pipeline never stalls.
- Write `.cache/buy_candidates.json`:
  `{ "generated_at": iso, "picks": [{ticker, rationale, strategies:[...]}] }`.

### Component 4 — watchdog buy source (small change)

`watchdog._get_screened_stocks()` (the candidate source for
`check_buy_signals`) reads `.cache/buy_candidates.json` when present (the agent's
top-4), else falls back to the current screener cache. Everything downstream —
the intraday timing test, dedupe-per-day, buy submission, Telegram
notification, and safety rails — is **unchanged**. The buy is auto-executed; no
approval prompt.

## Config additions (`config.py`)

```python
VS_MIN_DOLLAR_VOLUME = 2_000_000
VS_MIN_PRICE = 5.0
VS_MIN_MARKET_CAP = 300_000_000
VS_TOP_N = 20
VS_WEIGHTS = {"value": 0.5, "quality": 0.35, "improving": 0.15}
ENSEMBLE_TOP_N = 4               # agent's final picks
ENSEMBLE_STRATEGIES = ["value", "canslim"]   # registered, extensible
```

## Error handling

Fail-open throughout: a strategy that errors is skipped (others proceed); a
missing/corrupt strategy file is ignored; the agent falls back to rule-ranking
on any LLM failure; an empty pool yields an empty `buy_candidates.json` and the
watchdog simply finds nothing to buy. No path aborts the daily run.

## Testing (TDD)

- **value_screen:** gates exclude the right tickers; z-score/composite ordering;
  absent `improving` contributes 0; fail-open on empty `.info`.
- **strategies contract:** write/load round-trip; one corrupt file doesn't break
  `load_strategy_results`; a throwing strategy doesn't abort `run_strategies`.
- **agent select:** merge+dedupe keeps best rank + records strategies; rule
  pre-filter drops illiquid/owned; LLM-failure fallback returns rule-ranked
  top-N; output JSON shape validated; exactly `ENSEMBLE_TOP_N` picks.
- **watchdog source:** `_get_screened_stocks` prefers `buy_candidates.json`,
  falls back to screener cache when absent; downstream buy path unchanged
  (existing tests still pass).

## Rollout / verification

1. Unit tests pass.
2. Dry run: `run_strategies()` → two result files; `select_candidates()` →
   `buy_candidates.json` with 4 picks + rationale.
3. Confirm watchdog reads the 4 (dry, no HALT removal needed for read path).
4. Update `README.md` (ensemble pipeline, new modules, config, cron wiring).

## Build phases (for the implementation plan)

1. `value_screen.py` + the `strategies.py` contract/runner (+ canslim adapter).
2. `investor_agent.select_candidates` (+ rule fallback).
3. Watchdog candidate-source switch + cron wiring + README.
