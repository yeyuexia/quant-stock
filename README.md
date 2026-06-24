# Quantitative Investment System

A Python quant trading system for a $100K US-equity portfolio. Generates target allocations from dual-momentum ETF rotation, a CANSLIM technical stock screen with VCP base detection, and a FRED-based macro regime overlay, then submits orders to **Alpaca** (paper by default) through a safety-gated order layer. Two tranches: a $90K balanced core and a $10K leveraged-ETF aggressive sleeve. Core stock exits are driven by Minervini SEPA rules (R-multiple scale-out, failed-breakout, climax detection, 21EMA backstop).

## Quick Start

```bash
pip3 install -r requirements.txt

# Copy .env.example to .env and fill in:
#   FRED_API_KEY (free)                  https://fred.stlouisfed.org/docs/api/api_key.html
#   ALPACA_API_KEY + ALPACA_API_SECRET   https://app.alpaca.markets/paper/dashboard/overview
cp .env.example .env
# edit .env

# 1. See what the system would do (read-only analysis)
python3 run.py

# 2. Dry-run the rebalancer (prints plan, submits nothing)
python3 rebalancer.py --tranche core --dry-run
python3 rebalancer.py --tranche aggressive --dry-run

# 3. When ready, let it place orders on the paper account
python3 rebalancer.py --tranche core
```

## Architecture

```
   momentum.py / screener.py / macro.py / sentiment.py   ← signal modules
                         │
                         ▼
                  rebalancer.py          ← target-weight generation
                         │
                planner.py               ← tier + max_price + slice_count
                         │
        .cache/pending_plan.json
                         │
                executor.py (every 10 min, 5 circuit breakers)
                         │
             orders.py   ◄── watchdog.py (SEPA + macro exits)
                         │
                    broker.py             ← Alpaca SDK wrapper
                         │
                Alpaca  (paper | live)
                         │
     portfolio.json (cache)    pending_orders.json (Telegram queue)
```

**Invariant:** every order — from any trigger — passes through `orders.py`, which enforces HALT / paper-live guard / daily caps / large-order approval. `broker.py` is pure I/O and has no policy.

## Modules

| File | Role |
|---|---|
| `broker.py` | Thin Alpaca SDK wrapper. Raises `BrokerError` on any API failure; returns plain dataclasses (no SDK objects leak out). Reuses one `StockHistoricalDataClient` for all quote/trade fetches; caches `is_market_open` for 30s; `close_all_positions` is paper-only-guarded; `submit_bracket` takes absolute `stop_price` (caller computes from policy); `get_filled_notional` returns `None` on query failure so executor can distinguish "unfilled" from "unknown". Public method: `latest_price` (was `_latest_price` — old name kept as alias). |
| `orders.py` | Policy layer: `sync_state` (with 1-retry + cache fallback), `reconcile_to_targets` (with `MAX_POSITION_PCT` cap), `execute_plan` (greedy cash fill, defensive symbols sorted first), `submit_exit` / `submit_partial_exit` (accept `current_price` to avoid redundant broker calls), `ensure_trailing_stops`, pending-queue helpers (`approve_pending` re-checks cash + caps), `tag_position`. All disk writes go through `fileio.atomic_write_json` (fcntl lock + tmp-rename). Event log lives in `.cache/orders_events.csv` (distinct from watchdog's `daily_log.csv`). |
| `fileio.py` | Lock-protected JSON I/O helpers (`atomic_write_json`, `read_modify_write_json`, `atomic_append_text`) shared across orders / notifications / watchdog. Uses `.lock` sidecars + atomic tmp-rename. |
| `rebalancer.py` | Cron entry point. Builds target weights per tranche, runs the tiny portion through `orders.py`, writes the rest to `.cache/pending_plan.json`. |
| `planner.py` / `planning.py` | Enrich raw `OrderIntent`s with tier (HIGH/MED), `max_price` / `min_price`, and `slice_count`. Pure functions; no I/O. |
| `executor.py` | Intraday driver (every 10 min, 10:00–15:50 ET). Consumes the pending plan, evaluates five circuit breakers, submits sliced marketable-limit orders. Optimized for the daily-cadence world: tripped breakers skip re-evaluation; sticky aborts re-apply to mid-day merged intents; cross-tick news dedupe; one cached `get_open_orders` per tick; lazy SPY-15min fetch; local-clock RTH check; idle ticks skip disk writes; broker reads have 1-retry. |
| `notifications.py` | fcntl-locked append helper for `.cache/telegram_notifications.json` — used by rebalancer, executor, watchdog, and the quant subagent. Prevents concurrent-writer message loss. |
| `breakers.py` / `news_shock.py` / `pending_plan.py` | Circuit-breaker evaluators and the on-disk plan format read/written by rebalancer + executor. |
| `watchdog.py` | Daily monitor (8:30 ET) + lightweight intraday tick (every 5 min, RTH-gated). Reads live state via `orders.sync_state` with cache fallback on broker failure (transitions-only TG notification: degraded / recovered, no spam). Verifies brackets; routes SEPA exits (intraday only, with batched real-time `latest_quote` for fast R-tier triggers) and macro-driven exits (daily, act-before-persist so failed acts retry tomorrow). Buy signals dedup per day AND only stamp on successful submit so HALT'd / cash-gated signals still retry. Other optimizations: batched SEPA OHLCV (1 yf call vs N), batched buy-signal yfinance, batched daily `check_volume` + `check_portfolio_status`, U-shape intraday volume projection, append-only `daily_log.csv`, sidecar `.lock` files across all writes, single Broker instance per run. |
| `timeutils.py` | Shared `now_et()` + `is_rth_now()` helpers used by both executor and watchdog. |
| `sepa_exits.py` | Pure rule library for Minervini SEPA exits: R-multiple, failed-breakout, climax detection, 21EMA backstop. Watchdog calls into it. Hardened against corrupt position metadata (R ≤ 0 → None, bad entry_date / missing pivot → False+log not crash); `climax_check` takes explicit `symbol` arg so multi-ticker frames look at the right column; tuning defaults fall back to `config.SEPA_*` so callers can't drift; `config.SEPA_R_TIERS` must be monotonic-ascending in R (validated at config import). |
| `run.py` | Read-only daily reporter: `python3 run.py` (all sections) / `--section macro` / `--skip backtest` / `--with-review` (LLM cost, off by default) / `--backtest-years N`. Each section wrapped in `safe_section` — runtime errors print a "skipped" marker so partial output stays useful; bug-class exceptions (AttributeError / NameError / TypeError) re-raise so they're not silently swallowed. Risk analysis uses signals' real weights (renormalized over non-SAFE_HAVEN holdings); broker import is lazy so a broken SDK doesn't kill the report; ET-stamped header. |
| `config.py` | All parameters, mode selection, watchlist, safety caps. Merges `.cache/strategy_overrides.json` from the quant subagent at load time. Unknown `PORTFOLIO_MODE` raises (no silent fallback); list overrides (WATCHLIST etc.) have min/max length bounds; tests are isolated from local strategy overrides via the `_isolate_strategy_overrides` conftest fixture. `WATCHLIST` = the hand-curated `WATCHLIST_SEED` literal unioned with auto-discovered names loaded from `watchlist_auto.json` (fail-open if missing/corrupt). Public symbol: `config.ETF_LEVERAGED` (was `_ETF_LEVERAGED` — old name kept as alias). |
| `momentum.py` / `screener.py` / `sentiment.py` / `risk.py` / `backtest.py` / `discovery.py` / `indicators.py` / `baseline.py` / `investor_agent.py` | Signal and analytics modules. |
| `data.py` | yfinance fetch + cache layer. String tickers auto-wrap to `[...]` (no garbage cache keys); `fetch_ohlcv` always returns MultiIndex columns regardless of ticker count; all writes go through `fileio.atomic_write_*` (fcntl + tmp+rename); `fetch_info` is fail-open like `fetch_fundamentals` (consistent contract); `fetch_fundamentals` reuses `fetch_info`'s cache to avoid double `Ticker.info` round-trips; logger warnings on partial-failure paths so silent yfinance degradation is observable; 1-retry helper + module-level `ThreadPoolExecutor` shared across all timeout-guarded fetches. |
| `macro.py` | FRED-based regime detector. 1-retry on transient FRED errors; atomic CSV cache writes (fcntl + tmp+rename); proper Sahm Rule implementation (rolling 3m MA vs min of prior 12 3m MAs); yield-curve returns N/A on missing data (no magic 4.0 fallback); composite normalizes over only the indicators that returned data so partial outages don't dilute the score toward 0. CLI: `python3 macro.py` (print regime) / `python3 macro.py --refresh` (force-fetch all series). |
| `tests/` | `pytest` unit tests (615 tests across 47 files, ~18 s) + opt-in Alpaca paper integration tests (`-m integration`). One `test_X.py` per module (post-review additions are merged in under a `# Post-review additions` divider). Two topical extractions from the big `test_orders.py`: `test_orders_stops.py` (`_effective_stop_pct`) and `test_orders_partial_exits.py` (`submit_partial_exit`). End-to-end coverage in `test_executor_e2e.py`: one full-day quiet path plus one per circuit breaker (A SPY drop / B VIX spike / C single-name / D news shock / E macro flip). Shared `conftest._isolate_persistent_state` autouse fixture redirects every on-disk state path (portfolio.json, daily_log.csv, pending_*.json, watchdog sentinels, quant.applier artifacts, …) to a per-test tmp dir so runs can't pollute the developer's local `.cache/`. `FakeBroker` is strict-by-default — unseeded symbols raise; opt into a uniform price with `FakeBroker(default_price=…)`. |

## Commands

```bash
# Trading (Alpaca; paper by default)
python3 rebalancer.py --tranche core              # core tranche, daily cadence
python3 rebalancer.py --tranche aggressive        # aggressive tranche, daily cadence
python3 rebalancer.py --tranche core --dry-run    # plan without submitting
python3 rebalancer.py --tranche core --force      # bypass the same-day cadence gate

# Kill-switch
touch .cache/HALT                                  # pause all order logic
rm .cache/HALT                                     # resume

# Tag a position opened outside the system into a tranche
python3 -c "from orders import tag_position; tag_position('NVDA', 'core', 'manual 2026-04-17')"

# Read-only analysis (three modes)
python3 run.py                              # balanced (default)
PORTFOLIO_MODE=growth python3 run.py        # growth: leveraged ETFs + small/mid-caps
PORTFOLIO_MODE=conservative python3 run.py  # capital preservation

# Daily monitoring
python3 watchdog.py              # full: prices, stops, macro, news
python3 watchdog.py --quick      # price moves + bracket verification only
python3 watchdog.py --portfolio  # live positions + P&L from Alpaca

# Stock discovery
# Two-stage scan over a broad, stable universe:
#   Universe = config.DISCOVERY_UNIVERSE_INDICES constituents (S&P 500 +
#     Nasdaq-100 + S&P 400 MidCap from Wikipedia, ~916 large+mid-cap US names;
#     geo-neutral, with S&P 500 alone as fallback). Includes growth leaders
#     outside the S&P 500 (e.g. MRVL, via the Nasdaq-100).
#   Stage 1 (cheap): batch OHLCV -> universe-wide relative strength + liquidity
#     gate -> keep top DISCOVERY_STAGE1_KEEP survivors (watchlist/smart-money are
#     'protected' and always advance).
#   Stage 2 (expensive): info + fundamentals only for survivors -> composite rank.
#   Ranking: momentum/RS market-wide; value/quality within GICS sector; the top
#     growth cohort is exempt from the value-P/E penalty.
#
# Auto-discovered names live in watchlist_auto.json (config.WATCHLIST_AUTO_PATH),
# a GENERATED file. config.py loads it and unions it onto the hand-curated
# WATCHLIST_SEED literal to form config.WATCHLIST (seed first, then auto, deduped).
# --update / --prune touch ONLY watchlist_auto.json — they NEVER rewrite config.py,
# so the hand-curated WATCHLIST_SEED comments/grouping are preserved. A missing or
# corrupt watchlist_auto.json fails open (seed-only). Only valid tickers (alpha,
# dots/dashes ok, ≤5 chars) are accepted from the file.
python3 discovery.py                # scan market for new candidates
python3 discovery.py --trending     # list smart-money tickers (13F + ETF + ARK + Congress)
python3 discovery.py --include-reddit  # also harvest Reddit-trending tickers
python3 discovery.py --update       # scan + append top 50 to watchlist_auto.json
python3 discovery.py --prune        # list watchlist names not seen by CANSLIM in N days
python3 discovery.py --prune --confirm  # remove stale AUTO names from watchlist_auto.json (seed names + never-seen entries are kept)

# Tests
python3 -m pytest                              # unit tests only (integration deselected)
ALPACA_API_KEY=... ALPACA_API_SECRET=... \
  python3 -m pytest -m integration             # opt-in paper integration tests
```

## Safety Rails

The **paper/live guard** is enforced in `broker.py` at construction time: live trading requires **both** `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`. Any single-env typo keeps you on paper.

Every order then goes through five checks in `orders.execute_plan`, in this order:

1. **HALT file** (`.cache/HALT`) — one-line kill-switch. If present, all order logic exits cleanly and logs skipped intents. `touch .cache/HALT` to pause, `rm .cache/HALT` to resume.
2. **Market-open check** — orders submitted outside RTH are deferred to the next open.
3. **Cash-aware gate** — unless `ALLOW_MARGIN=True` (default `False`), the sum of all buy intents in a plan must not exceed available cash. If exceeded, **all** buys in that plan are rejected (sells are always allowed). A broker-account fetch failure also rejects buys (fail-closed).
4. **Daily caps** — `DAILY_MAX_ORDERS` (default 40) and `DAILY_MAX_NOTIONAL` (default **$200K paper / $25K live**) in `config.py`. Excess orders are deferred for the next day.
5. **Large-order approval** — orders ≥ `LARGE_ORDER_THRESHOLD` ($50K default) are queued to `pending_orders.json` instead of submitted. A Telegram bot (separate project) approves/rejects via `/pending`, `/approve <id>`, `/reject <id>`. Orders expire after `PENDING_ORDER_TTL_HOURS` (default 6).

All five checks apply uniformly to scheduled rebalances, SEPA exits, stop-loss exits, and signal-driven macro exits. Nothing bypasses them. The executor's per-slice path (`orders.submit_limit_slice`) enforces the same rails.

## Intraday Execution Layer

Rebalance orders are **not** submitted in a single burst at plan time. Instead:

1. **Planner** (`rebalancer.py`) builds a priced, ranked plan and writes it to `.cache/pending_plan.json`. Each intent carries a `tier` (HIGH/MED), `max_price` (buys) / `min_price` (sells), and a `slice_count` (2 or 4).
2. **Executor** (`executor.py`) fires every 10 min during market hours (10:00–15:50 ET). For each intent, it cancels the prior unfilled limit, evaluates five circuit breakers against the plan-time baseline, and submits the next slice as a marketable limit — if the ask (buy) or bid (sell) respects the price ceiling/floor.
3. **Circuit breakers** abort unexecuted work when the market stresses during the day:
   - **A: SPY drop** — −1.5% from baseline → abort all buys
   - **B: VIX spike** — >50% above baseline OR ≥25 absolute → abort all buys
   - **C: Single-name shock** — −5% on a symbol in the plan → abort that symbol only
   - **D: News shock** — keyword hit + SPY moved >0.5% in last 15 min → abort all buys
   - **E: Macro regime flip** — macro score drops ≥ 0.3 → abort risk-on buys (defensive BIL/SHY/IEF/TLT continue)
   Breakers are sticky: once tripped, the affected scope stays aborted for the rest of the day. **Sticky aborts also re-apply on every subsequent tick** — so a manual rebalancer run that merges new intents after a breaker fires will see those new intents aborted by the still-tripped breaker.
4. **End of day:** at 15:50 ET, any unfilled intent is canceled and marked `deferred`. Tomorrow's rebalancer re-validates against current signals.

See `docs/superpowers/specs/2026-04-17-intraday-execution-design.md` for the full design, thresholds, and rationale.

### Phased rollout

1. **Shadow mode** (`EXECUTOR_SHADOW_MODE = True` in `config.py`). Executor logs what it would submit without placing orders. Run 1–2 weeks on paper.
2. **Live on paper** (`EXECUTOR_SHADOW_MODE = False` — current default). Executor submits to the paper account. Run 2–4 weeks; tune circuit-breaker thresholds from real trips.
3. **Flip to live.** Follow the existing paper→live protocol (ramp `DAILY_MAX_NOTIONAL`).

## Quant Review Subagent

A daily LLM-driven strategy reviewer that runs via a Claude Code scheduled
remote trigger 3 hours after US-market close. Reviews portfolio state against
five external positioning signals, proposes parameter changes within a
risk-tiered allowlist, and reports everything via Telegram.

### Architecture

Single Claude Code remote trigger runs the workflow end-to-end:

1. `scripts/quant_fetch_portfolio.py` — dumps current portfolio state
2. `scripts/quant_fetch_externals.py` — fetches 5 external signals in parallel
   (13F filings, Reddit trending, popular ETF holdings, ARK daily trades,
   Congress/Pelosi STOCK Act disclosures)
3. Agent reasons, produces `.cache/proposed_changes.json`
4. `scripts/quant_apply.py` — classifies per risk tier, writes overrides/queue
   /Telegram notification/audit log

Low-risk changes (small stop-loss tweaks, watchlist additions) auto-apply.
High-risk changes (concentration shifts, screener filter changes) queue in
`.cache/strategy_proposals.json` for Telegram approval. Forbidden keys
(safety rails, credentials) are hard-rejected at two independent layers
(applier + `config.py` override loader).

### Setup

The trigger is created once via Claude Code's `schedule` skill:

```
/schedule create
```

Use the content of `quant/trigger_prompt.md` as the trigger prompt. Cron
schedule: `0 7 * * 2-6` (7 AM local Tue-Sat = 7 PM ET Mon-Fri, market
close + 3h). No `ANTHROPIC_API_KEY` needed — uses your CC subscription.

### Phased rollout

1. **Phase 0 — Dry-run** (~1 week). Trigger prompt includes `DRY_RUN=True`.
   Agent calls `quant_apply.py --dry-run` which writes
   `.cache/quant_review_dry.json` instead of the live files. TG report
   still sends. Review daily.
2. **Phase 1 — Live** (~2-4 weeks). Remove `DRY_RUN` from trigger prompt.
   Low-risk auto-applies; high-risk queues. Approve via direct JSON edit
   or (if bot ready) TG commands.
3. **Phase 2 — TG bot approval handlers** (separate repo).

### Files

| File | Purpose |
|---|---|
| `.cache/strategy_overrides.json` | Active overrides; read by `config.py` at module-load time |
| `.cache/strategy_proposals.json` | Pending high-risk queue; written by applier, consumed by TG bot |
| `.cache/telegram_notifications.json` | TG message queue (shared with executor-breaker notifications) |
| `.cache/proposed_changes.json` | Agent's intermediate output |
| `.cache/quant_review_dry.json` | Phase-0 dry-run artifact |
| `.cache/quant_review.log` | Append-only audit log |
| `quant/trigger_prompt.md` | Canonical version-controlled trigger prompt |

## State Model

**Alpaca is the source of truth** for cash, positions, market value, and unrealized P&L.

`portfolio.json` is a local cache that adds three things Alpaca doesn't know:
- `tranche` (`core` / `aggressive` / `unknown`) — which sleeve a position belongs to.
- `entry_reason` — audit trail (e.g., "core rebalance 2026-04-16").
- `tranches.{name}.last_rebalance` — cadence gating for the rebalancer.

On every run, `orders.sync_state(broker)` pulls live state from Alpaca, merges local metadata, and rewrites the cache. A position on Alpaca with no local metadata is tagged `unknown` and surfaced as an alert — you can tag it with `orders.tag_position(symbol, tranche)`. Deleting `portfolio.json` at any time is safe: it is rebuilt on the next run; only tranche tags are lost.

## Strategies

### Dual Momentum ETF Rotation (Antonacci 2014)

Ranks 19 US ETFs (+ 6 leveraged in growth mode) by a composite momentum score blending 1/3/6/12-month returns. Applies a 200-day SMA filter for absolute momentum. Holds the top-N ETFs equal-weighted; rotates to T-bills (BIL) when all signals turn negative.

**Selection hysteresis** — a held ETF that slips out of top-N is *kept* as long as it stays within top-(N + `MOMENTUM_HYSTERESIS_DEPTH`) AND remains above its 200-day SMA. Aggressive tranche uses the same rule with its own `AGGRESSIVE_PARAMS["hysteresis_depth"]`. This prevents whipsaw when an ETF oscillates around the rank cutoff under daily rebalance cadence — without it, a name flickering between rank 4 and rank 5 would be bought and sold on alternate days. Falling below the SMA still triggers an immediate sale (trend regime change overrides hysteresis). Set the depth to 0 to disable.

**ETF Universe:** SPY, QQQ, IWM, MDY, VTV, VUG, MTUM, QUAL, XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLRE, TLT, IEF, SHY
**Growth mode adds:** TQQQ, SOXL, UPRO, TNA, TECL, LABU

### CANSLIM Technical Stock Screen (+ VCP base detection)

Runs against `config.WATCHLIST` = the hand-curated `WATCHLIST_SEED` (≈50 tickers) unioned with auto-discovered names from `watchlist_auto.json` (appended by `discovery.py --update`); low-risk additions from the quant subagent still apply on top. Every survivor of the three technical hard gates (RS / ADR / EMA) is stamped into `.cache/discovery_lastpass.json` via `discovery.record_screener_pass`, which closes the loop with `discovery.py --prune` (lists watchlist names that haven't passed in ≥ `DISCOVERY_STALE_DAYS` = 90 days; `--confirm` removes them from `watchlist_auto.json` ONLY — hand-curated seed names and "never seen" entries are never pruned, and `config.py` is never rewritten). Two-stage filter, then composite ranking:

**Stage 1 — CANSLIM C+A fundamental hard gate** (fail-open when data missing):
- Quarterly EPS YoY ≥ `SCREEN_EPS_Q_GROWTH_MIN` (default 25%)
- Revenue YoY ≥ `SCREEN_REV_GROWTH_MIN` (default 20%)
- Annual EPS growing (most recent year > prior year, both positive)

**Stage 2 — Technical hard gate:**
- Relative Strength percentile (3M/6M/12M blend vs universe) ≥ `SCREEN_RS_MIN` (default 75)
- Average Daily Range ≥ `SCREEN_ADR_MIN` (default 4%) — tradability floor
- Price above both `SCREEN_EMA_FAST` and `SCREEN_EMA_SLOW` — trend filter

**Composite rank** for the survivors (Top-N → rebalancer picks Top-3):
- 40% ADR rank (volatility / opportunity)
- 40% EPS acceleration (latest QoQ growth > prior QoQ growth, both positive)
- 20% VCP-base score (≥ 2 strictly-decreasing peak-to-trough contractions, overall depth ≤ `SCREEN_BASE_DEPTH_MAX`, volume contracting on the right side). The last local peak becomes `vcp_pivot` — also persisted to `.cache/entry_pivots.json` on entry to drive failed-breakout exits.

### Macro Regime Overlay (FRED)

Six indicators scored from −1 (bearish) to +1 (bullish):

| Indicator | FRED Series | Signal |
|---|---|---|
| Yield Curve (10Y−2Y) | DGS10, DGS2 | Recession risk when inverted |
| Credit Spreads (HY OAS) | BAMLH0A0HYM2 | Financial stress |
| Unemployment + Sahm Rule | UNRATE | Labor market deterioration |
| Fed Funds Rate | FEDFUNDS | Monetary policy direction |
| Financial Conditions (NFCI) | NFCI | Tightening vs loosening |
| Market Breadth | SP500 | S&P 500 vs 200-day SMA |

The composite score adjusts equity allocation between 40%–100% of target. When the regime flips to contraction, the watchdog also exits the aggressive-tranche leveraged ETFs via `orders.submit_exit`.

## Exit Logic (Take-Profit + Stop-Loss)

Two layers: broker-side brackets (always-on hard stops) and watchdog-driven SEPA rules (daily, core stocks only).

### Broker-side (attached on entry)

- **Core**: initial stop = `clamp(ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close, ATR_STOP_FLOOR_PCT, STOP_LOSS_PCT)` — a 2×ATR(14) volatility stop, floored at 2% and capped at the fixed % stop. The floor stops near-zero-vol holdings from getting an absurdly tight ATR stop that fires on bid-ask noise. Defensive / safe-haven symbols (`DEFENSIVE_SYMBOLS`: BIL/SHY/IEF/TLT) skip ATR scaling entirely and use the base stop — you don't get stopped out of a cash-parking hedge. Trailing stop = `TRAILING_STOP_PCT` (balanced 12%). `ensure_trailing_stops` re-checks every watchdog run and re-attaches if missing.
- **Aggressive**: fixed `AGGRESSIVE_PARAMS["stop_loss_pct"]` 10% / trailing 15%. Tight because leveraged ETF decay is costly.

### Watchdog SEPA exits (`sepa_exits.py` + `watchdog.check_sepa_exits`)

Runs once per watchdog tick over each `core` position, in **strict priority order** — first rule that fires wins, the rest are skipped for that position this tick:

1. **Failed breakout (Phase 2)** — within `SEPA_FAILED_BREAKOUT_WINDOW_DAYS` (default 3) trading days after entry, if any in-window close < the stored entry pivot (`base_hi` from the screener) → cancel pending partials, cancel trailing, **full exit**. "If the breakout fails, get out today."
2. **Climax / blow-off top (Phase 2)** — all three must hold: cumulative return over `SEPA_CLIMAX_RETURN_LOOKBACK` (8) bars ≥ `SEPA_CLIMAX_RETURN_THRESHOLD` (25%), recent 20-bar mean daily range ≥ 2× the prior 20-bar mean, max volume in the last 3 bars ≥ 2× the prior 20-bar baseline. Action: sell **50% of current market value**, tighten trailing to `SEPA_CLIMAX_TRAIL_PCT` (6%), set `climax_fired=True` to disable further R-tier scale-outs.
3. **R-multiple scale-out (Phase 1)** — `R = initial_entry_price − initial_stop_price`. For each tier in `SEPA_R_TIERS` (default `[(2.0, 1/3), (3.0, 1/3)]`), once price has reached `entry + R × multiple`, sell that fraction of **initial** qty, cancel old trailing, re-attach trailing to the remaining qty. Skipped after climax fires.
4. **21EMA backstop (Phase 1 + 2)** — activated once the final R-tier has filled **or** climax has fired. If daily close < EMA(`SEPA_MA_PERIOD`=21) → **full exit** of whatever remains. Lets winners run during established trends; cuts when the trend breaks.

All SEPA exits route through the same `execute_plan` / `submit_exit` / `submit_partial_exit` paths and therefore inherit the five safety rails above. Notifications are queued to `.cache/telegram_notifications.json`.

`watchdog.check_price_moves` separately logs distance-from-entry and distance-from-peak warnings (informational; the broker brackets + SEPA rules do the actual exits).

## Portfolio Modes (Core Tranche)

Aggressive tranche is always leveraged-ETF-only, top-2, weekly rotation, 10% stop / 15% trail.

| Parameter | Conservative | Balanced | Growth |
|---|---|---|---|
| ETF / Stock split | 90% / 10% | 80% / 20% | 50% / 50% |
| Leveraged ETFs | No | No | Yes (3x) |
| Rebalance cadence | daily | daily | daily |
| Stop-loss (ceiling) | 6% | 8% | 12% |
| Trailing stop | 10% | 12% | 18% |
| Cash buffer (defensive only) | 10% | 5% | 3% |
| Top-N ETFs held | 3 | 4 | 3 |

**Rebalance cadence is daily** for all modes — `REBALANCE_BAND_PCT` (default 5% of tranche capital) is the actual churn brake: drifts smaller than the band are treated as holds. Same-day re-runs are still blocked.

**Cash buffer is *defensive* — not a constant**. The configured percentage only materializes as cash when macro stress pulls `etf_pct + stock_pct + cash_buffer` below 1.0 (i.e., the macro overlay shrinks equity exposure enough to leave room). In healthy regimes the ETF + stock allocations sum to ≥ 1.0 and the buffer is absorbed into deployed positions — by design. If you want a *constant* cash floor in bull markets too, raise `CASH_BUFFER_PCT` so it stays positive even after subtracting the configured ETF + stock allocations.

**Core stop-loss in the table is a ceiling** — the actual initial stop is `min(STOP_LOSS_PCT, 2 × ATR(14) / price)`, so quiet names get tighter stops. SEPA take-profit (R-multiple scale-out + 21EMA backstop + failed-breakout + climax) layers on top — see [Exit Logic](#exit-logic-take-profit--stop-loss).

**Core stock sleeve is robust to thin screener results.** When the CANSLIM screener returns nothing, the stock allocation flows to BIL (not silently to cash); when it returns fewer than `STOCK_SLEEVE_TOP_N` picks, per-stock weight is capped at `MAX_POSITION_PCT` and the remainder rolls to BIL. Entry pivots also fall back to the screening close when the pick has no clean VCP base, so SEPA failed-breakout always has a reference.

**Tranche capital is dynamic.** `core_capital = system_equity × 90%`, `aggressive_capital = system_equity × 10%`, where `system_equity = Alpaca account equity − unknown-tranche market value`. The system compounds as the account grows.

Set mode via environment variable:
```bash
export PORTFOLIO_MODE=growth
```

## Automation (cron, weekdays)

```bash
crontab -e
# Watchdog — 8:30 AM ET
30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py                        >> .cache/watchdog.log 2>&1

# Rebalancer — 9:35 AM ET daily for both tranches (post-open so baseline SPY/VIX reflect live levels).
# Cadence is daily; REBALANCE_BAND_PCT (5%) suppresses no-op churn.
35 9 * * 1-5 cd /Users/zl/works/stock && python3 rebalancer.py --tranche core       >> .cache/rebalance.log 2>&1
35 9 * * 1-5 cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive >> .cache/rebalance.log 2>&1

# Executor — every 10 min, 10:00–15:50 ET
*/10 10-15 * * 1-5 cd /Users/zl/works/stock && python3 executor.py                  >> .cache/executor.log 2>&1

# Discovery — weekly watchlist refresh, Sundays 8 AM ET (before Mon rebalance)
0 8 * * 0 cd /Users/zl/works/stock && python3 discovery.py --update                 >> .cache/discovery.log 2>&1
```

`rebalancer.py` writes `.cache/pending_plan.json` for orders ≥ `PLANNER_DIRECT_SUBMIT_THRESHOLD` (default $500) and direct-submits orders below that threshold. `executor.py` picks up the pending plan on the next 10-min tick, evaluates five circuit breakers, and slices orders across the day. Both scripts are safe to run daily: rebalancer no-ops unless the cadence threshold is reached, and executor no-ops if the pending plan is empty. The morning `watchdog.py` run is what drives daily SEPA take-profit / stop-loss exits — see [Exit Logic](#exit-logic-take-profit--stop-loss). The weekly `discovery.py --update` scans the multi-index universe (S&P 500 + Nasdaq-100 + S&P 400) and appends top names to `watchlist_auto.json`; the next rebalancer/screener tick consumes the expanded watchlist (`config.py`'s hand-curated `WATCHLIST_SEED` is never rewritten).

## Switching to live

Paper is the default. Before flipping:

1. Run on paper for several weeks. Review `daily_log.csv` and Alpaca's dashboard. Confirm brackets attach on every entry. Review any Telegram approval prompts that were wrong.
2. Set `DAILY_MAX_NOTIONAL` to a small number (e.g. $500) in `config.py`.
3. Export `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`.
4. Ramp `DAILY_MAX_NOTIONAL` over subsequent weeks as confidence grows.

## Data Sources

| Source | Cost | Used for |
|---|---|---|
| Alpaca | Free (paper), commission-free (live) | Brokerage — order submission, positions, account state |
| yfinance | Free | Historical prices, fundamentals, news (signal inputs) |
| FRED API | Free (key required) | Macro regime indicators |
| Reddit JSON | Free | Social sentiment from finance subreddits |
| Wikipedia | Free | S&P 500 component list |
| Wikipedia | Free | Nasdaq-100 + S&P 400 constituents — discovery scan universe (with S&P 500) |

## File Structure

```
stock/
  broker.py                  Alpaca SDK wrapper (pure I/O)
  orders.py                  Policy layer: sync, diff, safety rails, pending queue
  rebalancer.py              Cron entry point for core + aggressive tranches
  watchdog.py                Daily monitor + signal-driven exits
  run.py                     Read-only reporter
  config.py                  All parameters, modes, safety caps
  momentum.py                Dual-momentum ETF ranking
  screener.py                Value + quality stock screen
  macro.py                   FRED macro regime score
  sentiment.py               News + Reddit sentiment
  risk.py                    Portfolio analytics
  backtest.py                Historical backtest engine
  discovery.py               Stock discovery scanner
  data.py                    Market-data caching (yfinance)
  tests/
    fakes.py                 FakeBroker / FakeClock test doubles
    test_broker.py           Broker construction + live-confirm tests
    test_orders.py           Safety-rail unit tests (heaviest coverage)
    test_rebalancer.py       End-to-end with FakeBroker
    test_watchdog_smoke.py   Watchdog snapshot smoke test
    test_integration.py      Opt-in Alpaca paper tests
  requirements.txt
  pytest.ini
  .env                       API keys (not committed)
  .env.example               Template
  .cache/                    Market-data cache + HALT + daily_trade_log (not committed)
  portfolio.json             State cache rebuilt from Alpaca (not committed)
  pending_orders.json        Large-order approval queue (not committed)
  daily_log.csv              Daily equity + closed-position log (not committed)
  docs/superpowers/          Specs and implementation plans
```

## Requirements

- Python 3.9+
- Dependencies: `alpaca-py`, `yfinance`, `pandas`, `numpy`, `scipy`, `tabulate`, `fredapi`, `python-dotenv`, `pytest`, `pytest-mock` (see `requirements.txt`).

## Feedback / issues

Open an issue in this repo. Design docs are under `docs/superpowers/specs/`; implementation plans under `docs/superpowers/plans/`.
