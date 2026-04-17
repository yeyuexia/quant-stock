# Alpaca API Integration — Design Spec

**Status:** approved for implementation
**Date:** 2026-04-17
**Scope:** Replace manual order execution with fully-automated trading via Alpaca, gated by safety rails and Telegram approval for large orders. Paper-first, flipped to live via config flag.

---

## Goal

Today the system produces recommendations. A human reads `run.py` output, places orders at a broker, and edits `portfolio.json` by hand. This design makes the stock system the one that places and manages orders, with Alpaca as the broker and Telegram as the human-in-the-loop channel for orders above a size threshold.

### Success criteria

1. `rebalancer.py --tranche core` opens, closes, and re-weights positions to match the system's target allocation without human intervention for orders below the large-order threshold.
2. Stop-loss and trailing-stop protection is attached to every entry via Alpaca-native bracket orders — the stock system does not need to be running for stops to fire.
3. `watchdog.py` reads live position state from Alpaca and triggers signal-driven exits (e.g., macro regime flip) through the same safety-gated order path.
4. Every order, from every trigger source, passes through the safety layer: HALT file, daily cap (count + notional), large-order confirmation via Telegram.
5. Paper is the default environment; live requires two env vars (`ALPACA_ENV=live` **and** `ALPACA_LIVE_CONFIRM=yes`).
6. Deleting `portfolio.json` does not corrupt state — the next run rebuilds it from Alpaca.

### Non-goals

- No streaming / websocket / sub-second reaction. Cron cadence is sufficient for swing trading.
- No change to signal logic (`momentum.py`, `screener.py`, `macro.py`, `sentiment.py`). They remain the source of target allocations.
- No new backtesting changes. `backtest.py` remains offline.
- No CI wiring.
- No automatic position-tagging for positions opened outside the system (marked `unknown`, tagged by hand).
- Signal-driven *entries* happen on rebalance days only, not on every watchdog run. Watchdog may log "new candidate" alerts but won't open positions mid-cycle.

---

## Architecture

```
        config.py (+ALPACA_ENV, caps, HALT path, large-order threshold)
               │
   ┌───────────┼─────────────┐
   │           │             │
momentum.py  screener.py   macro.py / sentiment.py       ← unchanged
   │           │             │
   └─────┬─────┘             │
         ▼                   ▼
  rebalancer.py         watchdog.py
  (scheduled target-    (daily: stops, price
   weight generation)    moves, signal alerts)
         │                   │
         └─────────┬─────────┘
                   ▼
               orders.py           ← policy / safety layer
                   │
                   ▼
               broker.py           ← alpaca-py wrapper
                   │
                   ▼
          Alpaca (paper | live)
                   │
                   ▼
     portfolio.json (cache)   pending_orders.json (Telegram approval queue)
```

**Invariant:** every order Alpaca sees has been through `orders.py`. `broker.py` is purely I/O; callers of `broker.py` are limited to `orders.py` and tests.

### Files

**New**
- `broker.py` — Alpaca SDK wrapper. No policy, no retries, no caching.
- `orders.py` — policy, diffing, safety rails, pending-order queue management.
- `rebalancer.py` — cron-scheduled target-weight generator and plan executor.
- `pending_orders.json` — disk-backed queue of orders awaiting Telegram approval. Gitignored.
- `.cache/daily_trade_log.json` — UTC-day bucketed order count + notional. Gitignored.
- `tests/test_orders.py`, `tests/test_broker.py`, `tests/test_integration.py` — pytest.

**Modified**
- `config.py` — Alpaca env, daily caps, large-order threshold, HALT path.
- `watchdog.py` — reads state from Alpaca via `orders.sync_state`; signal-driven exits go through `orders.submit_exit`; verifies bracket orders are attached.
- `run.py` — becomes a read-only reporter; no longer mutates portfolio state.
- `requirements.txt` — adds `alpaca-py`, `pytest`, `pytest-mock`.
- `.gitignore` — adds `pending_orders.json`, `.cache/daily_trade_log.json`, `.cache/HALT`.
- `portfolio.json` — schema updated (see State Reconciliation).

---

## Module contracts

### `broker.py`

Thin SDK wrapper, one class `Broker`, constructor `Broker(env: str = "paper")`. No policy. Raises `BrokerError` on API failures. Return types are plain dataclasses — no raw SDK objects leak out.

```
get_account() -> AccountSnapshot(cash: float, equity: float, buying_power: float)
get_positions() -> list[Position(symbol, qty, avg_entry, market_value, unrealized_pl)]
get_open_orders() -> list[Order(id, symbol, side, type, qty|notional, status, client_order_id, parent_order_id)]
submit_market(symbol, *, notional: float|None, qty: float|None, side: "buy"|"sell", client_order_id: str) -> Order
submit_bracket(symbol, *, notional: float, stop_loss_pct: float, trailing_stop_pct: float, client_order_id: str) -> Order
cancel_order(order_id: str) -> None
close_all_positions() -> None        # test-setup helper; wraps Alpaca DELETE /v2/positions
is_market_open() -> bool
```

Live-mode construction refuses unless both `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes` are set; raises `ConfigError` otherwise.

### `orders.py`

Policy and orchestration. Imports `broker` and `config`. All trading decisions funnel through this module.

```
OrderIntent(symbol, notional, side, stop_pct, trail_pct, reason, client_order_id)
OrderPlan(buys: list[OrderIntent], sells: list[OrderIntent], holds: list[str])
ExecutionResult(submitted: list[Order], queued: list[OrderIntent], skipped: list[tuple[OrderIntent, str]])

sync_state(broker: Broker) -> PortfolioSnapshot
    # Fetches positions/account from Alpaca, merges local metadata
    # (tranche, entry_reason, stop IDs) from portfolio.json, writes
    # updated cache. Alerts on unknown positions and missing brackets.

reconcile_to_targets(targets: dict[str, float], tranche: str, snapshot: PortfolioSnapshot) -> OrderPlan
    # targets: {symbol: fraction_of_tranche_capital}
    # produces diff vs current holdings for that tranche.

execute_plan(plan: OrderPlan, reason: str) -> ExecutionResult
    # Runs every order through: HALT → market-open → daily caps →
    # large-order threshold. Submits or queues accordingly.

submit_exit(symbol: str, reason: str) -> ExecutionResult
    # For watchdog: sells a full position through the same gates.

approve_pending(order_id: str) -> ExecutionResult
    # Telegram callback; re-checks HALT + caps, then submits.
reject_pending(order_id: str) -> None
list_pending() -> list[PendingOrder]
tag_position(symbol: str, tranche: str, entry_reason: str = "manual") -> None
    # CLI helper for tagging unknown-tranche positions.
```

Tests mock `broker` with a `FakeBroker` that records calls in memory.

### `rebalancer.py`

New entry point. Uses existing signal modules.

```
python3 rebalancer.py --tranche core         # core tranche, 30-day cadence gate
python3 rebalancer.py --tranche aggressive   # aggressive tranche, 7-day cadence gate
python3 rebalancer.py --dry-run              # build plan, print, don't call broker
python3 rebalancer.py --force                # skip "is it rebalance day?" check
```

Flow:
1. `Broker(env=config.ALPACA_ENV)`.
2. `orders.sync_state(broker)` → `PortfolioSnapshot`.
3. Rebalance-day gate (`snapshot.tranches[tranche].last_rebalance` vs `config.REBALANCE_DAYS[tranche]`), unless `--force`.
4. Build targets:
   - `core`: `momentum.generate_signals()` + `screener.screen_stocks()` scaled by `macro.macro_risk_adjustment()`.
   - `aggressive`: `momentum.generate_signals()["leveraged_holdings"]`, top-N.
5. `orders.reconcile_to_targets(targets, tranche, snapshot)` → `OrderPlan`.
6. If `--dry-run`: print plan, exit. Otherwise `orders.execute_plan(plan, "{tranche} rebalance")`.
7. Update `last_rebalance` for that tranche in cache.

### `watchdog.py` changes

- `load_portfolio()` removed. Replaced with `orders.sync_state(broker)`.
- `check_portfolio_status()` now reads live Alpaca fields.
- Existing stop-loss / trailing-stop checks become *verification*: does each position have an attached bracket order? If not, alert. Do not manually submit sells for breached stops — Alpaca's brackets handle that.
- Macro regime shift check stays, but if it detects a bearish flip it calls `orders.submit_exit(symbol, reason="macro→contraction")` for leveraged ETFs in the aggressive tranche. Core tranche reacts only through its next scheduled rebalance (where `macro_risk_adjustment` reduces equity targets).
- New check: "candidate not in portfolio" — if screener surfaces a top pick not held, log as advisory; does not open position.

### `run.py` changes

Strip `run_portfolio_construction()` and order synthesis. Keep: macro, momentum ranking, stock screener, sentiment, backtest, risk analysis. Becomes `run.py` = "what would the system do right now?" read-only.

---

## Order flow

### Entry flow (rebalance day, core tranche)

```
cron (daily, 9:00 AM ET)
  └─ rebalancer.py --tranche core
       1. broker.get_account() + get_positions()
       2. orders.sync_state(broker)     → snapshot
       3. if (today - last_rebalance[core]) < 30: exit cleanly
       4. momentum.generate_signals() + screener.screen_stocks()
       5. macro.macro_risk_adjustment() scales equity allocation
       6. build targets dict:           {"SPY": 0.18, "QQQ": 0.15, ...}
       7. orders.reconcile_to_targets() → OrderPlan
       8. orders.execute_plan(plan, "core rebalance")
            ├─ HALT check
            ├─ market-open check
            ├─ daily-cap check
            ├─ large-order gate (≥ $2K → pending queue)
            └─ broker.submit_bracket(notional=$X, stop=0.08, trail=0.12)
       9. orders.sync_state(broker)     → rewrite portfolio.json cache
      10. update tranches.core.last_rebalance
      11. append snapshot to daily_log.csv
```

### Exit flow — stop-loss hit

Alpaca-native. Bracket order attached at entry fires automatically. No code path required. Watchdog's next run notices the position gone via `sync_state`, logs "closed" to `daily_log.csv`, and sends a Telegram alert.

### Exit flow — signal-driven (watchdog)

```
cron (8:30 AM ET weekday)
  └─ watchdog.py
       ├─ orders.sync_state(broker)
       ├─ check_macro_shift() → regime flipped to "contraction" today
       └─ for symbol in aggressive_leveraged_holdings:
             orders.submit_exit(symbol, reason="macro→contraction")
               └─ same HALT + caps + large-order gate as entries
```

### Client order IDs

Format: `{tranche}-{reason}-{symbol}-{YYYYMMDD}-{shorthash}`.
Example: `core-rebalance-SPY-20260417-a1b2c3`.

Alpaca rejects duplicate `client_order_id`. This makes any cron re-run idempotent — a partially-submitted plan can be safely re-executed; already-placed orders get rejected on the duplicate-ID path and `orders.py` logs and continues.

### Failures and partials

- `BrokerError` on submit → log, skip that intent, continue with the rest of the plan. Next cron retries.
- Bracket attach failure on an entry → position exists but is unprotected. Watchdog's next run alerts; `orders.py` exposes a `reattach_brackets(snapshot)` helper.
- Daily cap breached mid-plan → submits what fits. Remaining intents are *not* replayed as-is; they're recorded in `daily_trade_log.json` as `deferred` for the audit trail only. The next rebalancer run recomputes a fresh plan from current Alpaca state and new signals, and picks up whatever's still needed.
- Telegram bot down → large orders queue in `pending_orders.json`. Auto-expire after 6 hours. Watchdog re-plans on next run.

---

## Safety rails

Four layers in `orders.py`. Any one can veto an order. Checked in order. HALT first so a pause always wins.

### 1. HALT file

`config.HALT_PATH = ".cache/HALT"`. Both `execute_plan` and `submit_exit` check for the file before any other work. If present, log intended actions, push a single Telegram summary ("HALT active, N orders skipped"), return with `skipped` populated. Kill: `touch .cache/HALT`. Resume: `rm .cache/HALT`. Not cached across calls.

### 2. Paper / live guard

`broker.Broker.__init__` reads `ALPACA_ENV` from env (default `paper`). Live mode additionally requires `ALPACA_LIVE_CONFIRM=yes` — raises `ConfigError` if missing. Prevents a single-typo flip.

### 3. Daily caps

In `config.py`:

```
DAILY_MAX_ORDERS = 20            # count across all tranches
DAILY_MAX_NOTIONAL = 25_000      # $ across all tranches
LARGE_ORDER_THRESHOLD = 2_000    # Telegram approval threshold
PENDING_ORDER_TTL_HOURS = 6
```

State file: `.cache/daily_trade_log.json`, UTC-day bucketed.

```json
{
  "2026-04-17": {
    "submitted_count": 4,
    "submitted_notional": 8400.00,
    "deferred": [ {OrderIntent}, ... ]    // audit trail only; not replayed — see "Failures and partials"
  }
}
```

Before every submit, `orders.py` reads the bucket, adds the proposed order, rejects anything that would breach either cap. Excess intents move to `deferred` and the next cron picks them up.

### 4. Large-order confirmation (Telegram)

Orders with notional ≥ `LARGE_ORDER_THRESHOLD` are not submitted synchronously. They're written to `pending_orders.json`:

```json
[
  {
    "id": "ord_a1b2c3",
    "symbol": "TQQQ",
    "notional": 4500.0,
    "side": "buy",
    "stop_pct": 0.10,
    "trail_pct": 0.15,
    "reason": "aggressive rebalance",
    "tranche": "aggressive",
    "client_order_id": "aggressive-rebalance-TQQQ-20260417-a1b2c3",
    "created": "2026-04-17T14:32:00Z",
    "expires": "2026-04-17T20:32:00Z"
  }
]
```

Telegram bot responsibilities (owned by the existing telegram-bot project, not this one):
- `/pending` — list queued orders.
- `/approve <id>` — calls `orders.approve_pending(id)`; re-checks HALT + caps, then submits.
- `/reject <id>` — calls `orders.reject_pending(id)`; removes from queue.
- Push a Telegram message on plan completion when `queued` is non-empty.

Small orders (< threshold) submit immediately. On approval expiry, watchdog re-plans on its next run — no state lingers.

### Example: mixed-size rebalance

Plan: 5 buys of $800, $1,200, $3,000, $4,500, $2,500.

1. HALT check → pass.
2. Daily caps check → all 5 fit, pass.
3. $800 + $1,200 submit immediately as bracket orders.
4. $3,000 / $4,500 / $2,500 go to `pending_orders.json`.
5. Telegram bot pushes "3 orders pending approval".
6. User approves or rejects each from their phone. Each approval re-runs the caps check.

---

## State reconciliation

**Principle:** Alpaca is the only source of truth for `{cash, positions, shares, market value, unrealized P&L, open orders}`. `portfolio.json` becomes a cache plus local-only metadata.

### `portfolio.json` schema

```json
{
  "synced_at": "2026-04-17T14:32:00Z",
  "alpaca_env": "paper",
  "cash": 8992.35,
  "equity": 100104.12,
  "positions": [
    {
      "symbol": "MTUM",
      "shares": 63.0,
      "avg_entry": 269.99,
      "market_value": 17234.21,
      "unrealized_pl": 224.36,
      "tranche": "core",
      "entry_reason": "core rebalance 2026-04-16",
      "stop_order_id": "alpaca_ord_xyz",
      "trail_order_id": "alpaca_ord_abc"
    }
  ],
  "tranches": {
    "core":       {"last_rebalance": "2026-04-16"},
    "aggressive": {"last_rebalance": "2026-04-16"}
  }
}
```

Fields sourced from Alpaca per-run: `cash`, `equity`, `shares`, `avg_entry`, `market_value`, `unrealized_pl`.
Fields local-only: `tranche`, `entry_reason`, `stop_order_id`, `trail_order_id`, `tranches.*.last_rebalance`.

### `orders.sync_state(broker)` algorithm

1. `pos = broker.get_positions()`; `acc = broker.get_account()`; `open = broker.get_open_orders()`.
2. Load old `portfolio.json` for local metadata.
3. For each Alpaca position:
   - If symbol was in old cache → carry forward `tranche`, `entry_reason`.
   - If new → `tranche = "unknown"`, `entry_reason = "external"`; queue Telegram alert.
4. For each cached symbol not in Alpaca → append close event to `daily_log.csv`; drop from new snapshot.
5. Reconcile brackets: for each position, find attached stop/trail from `open`. If missing → queue Telegram alert.
6. Write updated `portfolio.json`.
7. Append equity snapshot to `daily_log.csv`.

### Unknown-tranche positions

Rebalancer ignores them for diffing (neither sells nor counts against caps). Watchdog surfaces them in alerts. Tag manually:

```
python3 -c "from orders import tag_position; tag_position('NVDA', 'core', 'manual entry 2026-04-17')"
```

Deliberately simple — no auto-classification.

### Migration from current `portfolio.json`

Today's `portfolio.json` has 9 positions dated 2026-04-16 with per-share entry prices. On first run:

- If Alpaca paper account is empty: rebalancer at next cadence opens positions fresh. Matches the current recommendations up to pricing drift.
- If user has mirrored positions into Alpaca by hand: `sync_state` will see them, tag `unknown`, alert via Telegram. User runs `tag_position` for each to set tranche + entry_reason.

Suggested onboarding order:
1. Create Alpaca paper account, set `ALPACA_API_KEY` + `ALPACA_API_SECRET` in `.env`.
2. `python3 rebalancer.py --tranche core --dry-run` — read the plan.
3. `python3 rebalancer.py --tranche aggressive --dry-run` — read the plan.
4. If plans look right, remove `--dry-run`. System opens positions fresh on paper.
5. Delete the legacy `portfolio.json` (it will be rebuilt).

### Idempotency

Every decision is derived from Alpaca state, never from cache. Delete `portfolio.json` at any time — next run rebuilds it. Only tranche tags and `entry_reason` are lost (recoverable via `tag_position`).

---

## Configuration

Additions to `config.py`:

```python
# ── Alpaca ──────────────────────────────────────────────────────
ALPACA_ENV = os.environ.get("ALPACA_ENV", "paper")       # paper | live
ALPACA_LIVE_CONFIRM = os.environ.get("ALPACA_LIVE_CONFIRM") == "yes"
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")        # from .env
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")  # from .env

# ── Safety rails ────────────────────────────────────────────────
HALT_PATH = os.path.join(os.path.dirname(__file__), ".cache", "HALT")
DAILY_TRADE_LOG = os.path.join(os.path.dirname(__file__), ".cache", "daily_trade_log.json")
PENDING_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "pending_orders.json")

DAILY_MAX_ORDERS = 20
DAILY_MAX_NOTIONAL = 25_000
LARGE_ORDER_THRESHOLD = 2_000
PENDING_ORDER_TTL_HOURS = 6

# ── Rebalance cadence ───────────────────────────────────────────
REBALANCE_DAYS = {
    "core": _params["rebalance_days"],              # 30 balanced, 14 growth, 30 conservative
    "aggressive": AGGRESSIVE_PARAMS["rebalance_days"],  # 7
}
```

---

## Testing

Three layers, in priority order.

### 1. Unit tests (`tests/test_orders.py`) — safety layer

The part that costs money when buggy. `broker.py` mocked via an in-memory `FakeBroker`.

Must-have cases:
- HALT file present → `execute_plan` submits nothing, returns all as skipped.
- Daily order-count cap breached mid-plan → submits until cap, remainder deferred.
- Daily notional cap breached mid-plan → same.
- Order notional ≥ threshold → queued, not submitted.
- Duplicate `client_order_id` → `BrokerError` caught, logged, plan continues.
- `submit_exit` goes through caps (not a backdoor).
- `sync_state` with unknown position → tranche = `"unknown"`, Telegram alert emitted.
- `sync_state` with missing bracket order → Telegram alert emitted.
- `approve_pending` on expired order → rejected cleanly, order removed from queue.
- `Broker(env="live")` without `ALPACA_LIVE_CONFIRM=yes` → `ConfigError`.
- `reconcile_to_targets` ignores unknown-tranche positions for diffing.

### 2. Integration tests (`tests/test_integration.py`) — against Alpaca paper

`@pytest.mark.integration`, skipped unless `ALPACA_API_KEY` set. Each test calls `broker.close_all_positions()` at setup (Alpaca has a `DELETE /v2/positions` endpoint) to reset state.

Covers:
- `broker.submit_bracket` → order appears on Alpaca, stop attached, cancel round-trips.
- `rebalancer.py --tranche core --force` on empty paper account → positions open, brackets attached, `portfolio.json` matches Alpaca.
- Watchdog signal-driven exit → position sold via the same code path.

Run on-demand. `make test` = unit only; `make test-integration` = both.

### 3. Staged rollout checklist (manual, not automated)

Documented as an explicit gate before flipping to live. Keep this list in the PR description for the implementation plan:

- Week 1-2: paper, `rebalancer.py --dry-run` only. Read every plan.
- Week 3-4: paper, real submits, cron on, Telegram approvals enabled. Watch `daily_log.csv`.
- Week 5+: review paper performance vs. backtest expectations. Any tranche drift > 5%? Any stop that didn't fire when it should have? Any Telegram prompt that was wrong?
- Only then: `ALPACA_ENV=live ALPACA_LIVE_CONFIRM=yes`, `DAILY_MAX_NOTIONAL` set to $500 for week 1, ramp from there.

### Out of scope for testing

- Signal correctness in `momentum.py`, `screener.py`, `macro.py`. Existing code, unchanged.
- Alpaca's order-routing behavior.
- `backtest.py` / `run.py` beyond smoke tests (behavior-preserving changes only).

### Tooling

Add `pytest` and `pytest-mock` to `requirements.txt`. Test runner: `python3 -m pytest tests/`. No CI.

---

## Open operational questions (for the implementation plan to resolve)

- Exact Alpaca SDK version pin (`alpaca-py` latest stable at implementation time).
- `.env` key names — this spec assumes `ALPACA_API_KEY` / `ALPACA_API_SECRET`; the SDK supports others.
- How the Telegram bot polls / subscribes to `pending_orders.json`. Options: bot polls the file every minute, or `orders.py` POSTs to a local endpoint the bot exposes. Bot owner decides.
- Whether to add a `--tranche both` mode to `rebalancer.py` for combined runs.
- Whether to gate `run.py` behind `ALPACA_API_KEY` set (read-only reporter that shows live P&L), or keep it fully offline.
