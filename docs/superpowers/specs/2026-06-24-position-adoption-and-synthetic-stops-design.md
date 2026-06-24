# Design: Position adoption + synthetic enforced stops

**Date:** 2026-06-24
**Status:** Approved (design)
**Scope:** Fix issue #1 (frozen book / rebalancer capital starvation) and issue #2
(no active stop protection), so neither can silently recur.

## Background

A 3-week review of trading history surfaced that the live book has been frozen
for 10+ consecutive sessions (identical holdings, cash byte-for-byte unchanged at
`2896.71`) and carries **zero active stop protection** on all 14 positions.

Both problems trace to one root cause: positions imported from the broker are
never reconciled into a sleeve. They are tagged `tranche: "unknown"`,
`entry_reason: "external"`, with `stop_order_id: null`.

Two distinct failures result:

1. **Frozen book (#1).** `rebalancer._system_equity` computes addressable capital
   as `equity − unknown_mv`. With all 14 positions "unknown"
   (`unknown_mv ≈ 124,226`, `equity ≈ 127,126`), the rebalancer believes it has
   only ~`$2,900` to manage — exactly the idle cash. It correctly does nothing, so
   the strategy is effectively offline. The reported +31% P&L is unmanaged beta.

2. **No active stops (#2).** Two compounding causes:
   - **All 14 positions are fractional** (e.g. AMD 11.188 sh). Alpaca rejects
     native stop / trailing-stop / bracket orders on fractional quantities, so
     `orders.ensure_trailing_stops` silently fails (BrokerError) on every one.
   - The watchdog's stop check (`check_price_moves`, line ~264) only *alerts*
     ("SELL NOW") — it never sells. Protection is advisory, not active.

## Goals

- The rebalancer addresses the full book; an externally-imported position can
  never again starve it into inactivity.
- Every position has working stop protection that functions on fractional shares.
- A defense-in-depth alert fires if the starvation condition ever recurs.
- All behavior changes are gated by config flags (reversible, testable).

## Non-goals

- Concentration / hidden-leverage de-risking (issue #3).
- Cleaning corrupt historical `daily_log.csv` rows (issue #4).
- Changes to rebalancer target-construction logic.

## Design

### Part A — Auto-adopt external positions (fixes #1 root cause)

In `orders.sync_state` — the single reconciliation chokepoint, called by both
`rebalancer.run` and `watchdog.snapshot` — when a live position has no local
metadata (`meta is None`):

- Classify into a sleeve:
  - `aggressive` if `symbol in config.ETF_LEVERAGED`
  - `core` otherwise
- Set `tranche = <classified>`, `entry_reason = "adopted"`.
- Persist through the cache write `sync_state` already performs.
- Keep an alert, reworded as informational:
  `"Adopted external position X into <sleeve>."`

Gated by `config.ADOPT_EXTERNAL_POSITIONS = True`. When `False`, the prior
behavior (`tranche = "unknown"`, `entry_reason = "external"`) is preserved.

Effect: `rebalancer._system_equity` no longer subtracts these positions, so the
rebalancer sees the full equity and resumes rotation.

### Part B — Starvation guardrail (defense-in-depth for #1)

After reconciliation, compute `unknown_mv / equity`. If it exceeds
`config.UNKNOWN_MV_HALT_PCT` (default `0.20`), append a `CRITICAL` alert:
`"Untagged positions are <pct>% of equity — rebalancer capital starved."`

This is the safety net for the case where adoption is disabled or a symbol can't
be classified. It is an alert (surfaced by the watchdog), not an auto-HALT —
Part A already prevents the condition in normal operation.

### Part C — Synthetic enforced stop (fixes #2)

Because native stops cannot attach to fractional shares, enforcement moves into
the daily watchdog stop-check path (`check_price_moves`). When a position breaches
its level:

- Trigger condition (unchanged thresholds): `from_entry <= -stop_loss_pct`
  **or** `from_peak <= -trail_stop_pct`, using the position's sleeve %s
  (core vs aggressive).
- Action: submit a **market sell of the full position** via
  `broker.submit_market(symbol, qty=<full>, side="sell", client_order_id=...)`.
  Market orders accept fractional quantities, so this works where native stops
  cannot.
- Log a `CLOSED` event to `.cache/orders_events.csv`.
- Respect the HALT file (no selling when halted).
- Gated by `config.ENFORCE_STOPS = True`. When `False`, behavior is the current
  alert-only path (preserved exactly).

Modeled on the existing active-selling pattern in `watchdog.check_sepa_exits`.

`orders.ensure_trailing_stops` is left in place (best-effort; it already swallows
fractional rejections) but is no longer the primary protection.

### Part D — One-time reconciliation of current state

No separate script. The next `sync_state` run auto-adopts the existing 14
positions (Part A); the watchdog then governs their stops (Part C). Verify via a
dry run before relying on it.

## Config additions

```python
ADOPT_EXTERNAL_POSITIONS = True   # auto-tag broker-imported positions into a sleeve
UNKNOWN_MV_HALT_PCT       = 0.20  # alert if untagged MV exceeds this share of equity
ENFORCE_STOPS             = True  # watchdog market-sells on stop/trailing breach
```

## Testing (TDD)

- **orders / adoption:** external position with no meta → adopted to `core`;
  leveraged-ETF symbol → `aggressive`; flag off → remains `unknown`/`external`.
- **rebalancer:** after adoption, `_system_equity` ≈ full equity (not starved).
- **guardrail:** `unknown_mv/equity` above threshold → `CRITICAL` alert present.
- **watchdog stops:** breach with `ENFORCE_STOPS=True` → `submit_market` called
  with full qty + `CLOSED` logged; `ENFORCE_STOPS=False` → alert-only (current
  behavior); HALT present → no sell.

## Rollout / verification

1. Unit tests above pass.
2. Dry-run `sync_state` against current `portfolio.json` → confirm all 14 adopt
   to expected sleeves and `_system_equity` ≈ full equity.
3. Update `README.md` (config flags + new watchdog enforcement behavior).
