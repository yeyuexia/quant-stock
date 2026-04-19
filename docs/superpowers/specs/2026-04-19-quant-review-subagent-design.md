# Quant Review Subagent — Design

**Date:** 2026-04-19
**Status:** Spec, awaiting implementation plan
**Scope:** A daily LLM-driven strategy review that runs 3 hours after US-market close, compares the portfolio against five external datasets (13F filings, Reddit, popular ETFs, ARK/Cathie Wood daily trades, Congress/Pelosi disclosures), proposes parameter adjustments within a safety-gated allowlist, and reports everything (applied or queued) to Telegram with full rationale.

---

## 1. Motivation

The trading system is fully automated end-to-end: signals → rebalancer → executor → broker, with safety rails at every step. What it currently lacks is an **overnight judgment layer** — a senior-quant perspective that reviews the day's close state in the context of external positioning signals (what smart money is doing, what retail is loud about, what ARK/Pelosi filed today) and nudges strategy parameters in response.

The user wants this review to be autonomous — low-risk adjustments apply automatically, high-risk ones queue for Telegram approval. Every change (auto or queued) reports to Telegram with full reasoning and a detailed plan.

Three principles shape the design:

1. **Nothing it does can bypass the existing safety rails.** Every trade still goes through `orders.py`. The subagent only adjusts parameters that feed into the existing pipeline; the pipeline's HALT / daily-caps / large-order / circuit-breaker layers remain the last line of defense.
2. **No code mutation.** The subagent writes JSON. `config.py` reads the JSON and applies allowlisted keys. An LLM writing Python code into the repo unattended is explicitly out of scope.
3. **Default to do-nothing.** On any failure (API down, data source failure, malformed output, out-of-bounds proposal), the system reverts to yesterday's config. The review is enhancement-only; the trading system must work without it.

---

## 2. Goals & non-goals

### Goals

- Daily (Tue–Sat, 7 AM local / 7 PM ET) autonomous strategy review using a Claude Code remote trigger (subscription, no API billing).
- Synthesize five external data sources with the current portfolio state to produce structured proposals.
- Auto-apply low-risk changes (stop-loss nudges, watchlist additions) within tight bounds.
- Queue high-risk changes (concentration shifts, screener filter changes) for Telegram approval.
- Hard-reject any proposal touching safety rails, credentials, or infrastructure paths.
- Telegram report every run — rationale + detailed plan + effect + confidence per change.
- Phased rollout matching the shadow-mode pattern: dry-run → live-apply → with-bot-approval.

### Non-goals (v1)

- **No Anthropic API integration.** Uses Claude Code subscription via scheduled remote trigger.
- **No code generation.** The subagent can never write or modify Python code.
- **No safety-rail adjustment.** `DAILY_MAX_ORDERS`, `CIRCUIT_BREAKERS`, `EXECUTOR_SHADOW_MODE`, credentials, and paths are forbidden keys.
- **No cross-day memory.** Each run is stateless. Past proposals are logged for audit but not fed back into the prompt (prevents feedback loops).
- **No signal-weight optimization** (changing how momentum/screener/macro combine). Possible future work.
- **No live fundamentals** (earnings calendars, EPS surprises). Add later if the LLM keeps asking.
- **Telegram bot command handlers** (`/strategy-approve` etc.) — that's the separate bot repo's work. This repo only writes the queue files.

---

## 3. Architecture

### One scheduled remote trigger, pure data-flow design

The review is a **Claude Code remote trigger** created once via the `schedule` skill with cron `0 7 * * 2-6`. On fire, Claude Code spawns a fresh, non-interactive agent session with the trigger prompt. The agent has standard CC tools (Bash, Read, Write, Grep) but no user to answer questions — it must run the workflow end-to-end autonomously. Agent runs data-fetch scripts, reasons, writes output JSONs, session ends.

The Python codebase provides three CLI-entry-point helper scripts and two library modules. **No `quant_review.py` orchestrator** — the agent orchestrates via tool calls.

### File structure

**New:**

```
quant/
  __init__.py
  data_sources.py     — 5 external-signal fetchers, returns ExternalSignal dataclasses
  applier.py          — risk-tier classification + file I/O
  schema.py           — ProposedChange, QuantReview, ExternalSignal dataclasses

scripts/
  quant_fetch_portfolio.py   — CLI: dumps current portfolio JSON to stdout
  quant_fetch_externals.py   — CLI: runs all 5 fetchers in parallel, dumps JSON to stdout
  quant_apply.py             — CLI: takes a proposals JSON path, applies/queues/rejects,
                               writes the three output files
```

**Modified:**

```
config.py   — append override-loader block at end (reads .cache/strategy_overrides.json)
.env        — no new entry needed (subscription auth is handled by CC)
```

**Runtime artifacts (in .cache/):**

```
strategy_overrides.json     — active overrides; written by applier + TG bot; read by config.py
strategy_proposals.json     — pending high-risk queue; written by applier; mutated by TG bot
telegram_notifications.json — daily TG report queue (shared with executor-breaker notifications)
quant_review.log            — append-only audit of every review run
quant_review_dry.json       — Phase-0 artifact only
proposed_changes.json       — agent's intermediate output; consumed by applier
portfolio_state.json        — intermediate cache from quant_fetch_portfolio.py (optional)
external_signals.json       — intermediate cache from quant_fetch_externals.py (optional)
```

### Data flow

```
7 AM local Tue–Sat  (= 7 PM ET Mon–Fri, market close + 3h)

   Claude Code remote trigger fires
           │
           ▼
   Fresh CC agent session with the trigger prompt
           │
           ├─ Bash: python3 scripts/quant_fetch_portfolio.py
           │       → portfolio JSON (positions, cash, equity, P&L, open orders)
           │
           ├─ Bash: python3 scripts/quant_fetch_externals.py
           │       → 5-signal JSON (13F, Reddit, popular ETFs, ARK, Congress)
           │         each stamped with `as_of` freshness
           │
           ├─ Read: .cache/strategy_overrides.json
           │       → current active overrides (so agent doesn't re-propose same change)
           │
           ├─ Agent reasons. Produces ProposedChange list (zero or more).
           │       Default: zero changes unless a specific signal justifies one.
           │
           ├─ Write: .cache/proposed_changes.json
           │
           ├─ Bash: python3 scripts/quant_apply.py .cache/proposed_changes.json
           │       applier:
           │         - validates schema (rejects malformed items)
           │         - classifies per risk tier (low / high / forbidden)
           │         - enforces bounds (rejects out-of-band low-risk proposals)
           │         - low-risk → merges into .cache/strategy_overrides.json
           │         - high-risk → appends to .cache/strategy_proposals.json
           │         - forbidden → rejects, surfaces in TG report
           │         - writes .cache/telegram_notifications.json (rich report)
           │         - writes to .cache/quant_review.log (append-only)
           │
           └─ Agent confirms by re-reading the three output files, summarizes
             in its own final response for audit; session ends.
```

### Key invariants

- **No order-path changes.** The subagent never calls `orders.py`, `broker.py`, or the executor. It only writes config overrides.
- **`config.py` is the gatekeeper.** Its override loader has its own allowlist — if the JSON contains a forbidden key, the override block logs and ignores. The applier is not the only line of defense.
- **Default-deny.** Keys not in the allowlist are always ignored.
- **Stateless per run.** Agent reads disk, reasons, writes disk, exits. No memory across runs.

---

## 4. Data sources

Each fetcher returns a normalized `ExternalSignal`:

```python
@dataclass(frozen=True)
class ExternalSignal:
    source: str              # "13F" | "reddit" | "etf-holdings" | "ark" | "congress"
    as_of: dt.datetime       # freshness timestamp
    data: list[dict]         # source-specific normalized rows
    error: Optional[str] = None   # set if fetch failed; data=[] in that case
```

Each fetcher catches all exceptions and returns a signal with `error=...`. The prompt builder always gets five signals; the LLM sees which ones had gaps and calls them out in the report.

### The five fetchers

| # | Source | Access | Cadence | Extraction |
|---|---|---|---|---|
| 1 | **13F filings** | SEC EDGAR free API + HTML fallback | Quarterly, filed 45 days after quarter end | Curated fund list: Berkshire, Bridgewater, Tiger Global, Renaissance, Citadel, Third Point. For each: new positions, increases, reductions, exits. |
| 2 | **Reddit trending** | Extend existing `sentiment.py` helpers | Real-time (last 24h) | Top 20 mentioned tickers with bullish/bearish word counts + top 3 post titles each. |
| 3 | **Popular ETFs** | yfinance (free) | Daily | Top 25 holdings of MAGS, ARKK, QQQ, ICLN, VGT with weights. |
| 4 | **ARK / Cathie Wood** | Scrape `ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_Trades_*.csv` | Daily after close | Last 7 days of ARK trades across ARKK/ARKG/ARKQ/ARKW/ARKF: ticker, direction, shares, fund. |
| 5 | **Pelosi / Congress** | `capitoltrades.com` free JSON endpoint | Irregular (disclosures within 45 days of trade, per STOCK Act) | Last 14 days of disclosed trades for Pelosi + frequently-traded members: member, ticker, direction, amount range, disclosed date. |

Fetchers run in parallel via ThreadPoolExecutor with 30s per-source timeout. `quant_fetch_externals.py` ensures all five always return a signal — healthy or error.

---

## 5. Override mechanism + risk tiers

### Override mechanism

At the end of `config.py`, a single block reads `.cache/strategy_overrides.json` and applies allowlisted keys:

```python
# config.py (appended)
import json, logging
_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), ".cache", "strategy_overrides.json")
_OVERRIDE_SCHEMA = {
    # key → (type, lower_bound, upper_bound)
    "WATCHLIST":            (list,  None, None),
    "NEWS_SHOCK_KEYWORDS":  (list,  None, None),
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
    "CASH_BUFFER_PCT":      (float, 0.02, 0.20),
}
if os.path.exists(_OVERRIDES_PATH):
    try:
        with open(_OVERRIDES_PATH) as f:
            _overrides = json.load(f)
    except Exception as e:
        logging.warning(f"config: corrupt strategy_overrides.json: {e}; using defaults")
        _overrides = {}
    for k, v in _overrides.items():
        if k not in _OVERRIDE_SCHEMA:
            logging.warning(f"config: ignoring override for unknown key {k!r}")
            continue
        expected_type, lo, hi = _OVERRIDE_SCHEMA[k]
        if not isinstance(v, expected_type):
            logging.warning(f"config: override for {k!r} has wrong type {type(v).__name__}")
            continue
        if lo is not None and (v < lo or v > hi):
            logging.warning(f"config: override for {k!r} out of bounds [{lo},{hi}]")
            continue
        globals()[k] = v
```

This is default-deny: an LLM-proposed `DAILY_MAX_NOTIONAL = 500_000` never makes it in, because the key isn't in `_OVERRIDE_SCHEMA`.

### Low-risk tier (auto-apply, no approval)

| Key | Bound |
|---|---|
| `WATCHLIST` | **Additions only** (applier rejects removals); net list size ≤ 100 |
| `NEWS_SHOCK_KEYWORDS` | **Additions only**; net list size ≤ 30 |
| `STOP_LOSS_PCT` | Within ±20% of current AND [0.04, 0.20] |
| `TRAILING_STOP_PCT` | Within ±20% of current AND [0.06, 0.25] |
| `CASH_BUFFER_PCT` | Within ±50% of current AND [0.02, 0.20] |

The applier enforces the relative-pct bounds; `config.py`'s loader enforces the absolute bounds as a second line of defense.

**"Current" semantics:** the ±20% / ±50% bands are measured against the value as currently effective at apply-time (i.e., `getattr(config, key)` — which includes any existing override). The bands limit per-run movement, not cumulative drift from the repo default. Over many days, values can legitimately drift outside the band relative to the config.py default if every day's signal supports one more step — this is intentional (parameter adaptation), and the user sees cumulative drift in the TG report across days. To revert to the repo default, the user runs `/strategy-revert <key>` in the TG bot, which removes the key from `strategy_overrides.json`.

### High-risk tier (requires approval)

| Key | Why it's risky |
|---|---|
| `WATCHLIST` removals | User may have put a stock there intentionally |
| `NEWS_SHOCK_KEYWORDS` removals | Shrinks Breaker D coverage |
| `MOMENTUM_TOP_N` | Changes portfolio concentration |
| `ETF_ALLOCATION_PCT` / `STOCK_ALLOCATION_PCT` | Shifts core tranche composition |
| Out-of-band changes to `STOP_LOSS_PCT`, `TRAILING_STOP_PCT`, `CASH_BUFFER_PCT` | Bigger-than-band swings |
| `SCREEN_MIN_ROE` / `SCREEN_MAX_PE` / `SCREEN_MAX_DEBT_EQUITY` | Changes stock-screener filter |
| `MOMENTUM_LOOKBACK_MONTHS` | Alters signal definition |
| `SAFE_HAVEN` | Changes risk-off ETF target |

Proposal format in `.cache/strategy_proposals.json`:

```json
[
  {
    "id": "prop_2026-04-19_01",
    "key": "MOMENTUM_TOP_N",
    "current": 4,
    "proposed": 3,
    "rationale": "Concentration improves Sharpe in trending regimes; macro firmly risk-on at +0.45.",
    "detailed_plan": "At next rebalance, drop rank-4 holding (MTUM, ~$15K). Redistribute proportionally to rank-1/2/3. Concentration in top-3 rises 60% → 75%.",
    "created_at": "2026-04-19T07:00:00+08:00",
    "expires_at": "2026-04-19T21:35:00+08:00"
  }
]
```

**Expiry:** at the next rebalancer run (21:35 local). Un-approved proposals drop. If the signal still holds, next day's review re-proposes.

### Forbidden tier (hard reject)

Any key not in the low/high allowlists. Explicitly called out in the trigger prompt so the LLM doesn't waste tokens proposing changes that will be rejected:

- `DAILY_MAX_ORDERS`, `DAILY_MAX_NOTIONAL`, `LARGE_ORDER_THRESHOLD`
- Any key in `CIRCUIT_BREAKERS`
- `HALT_PATH`, all `.cache/` paths, all file-location constants
- `ALPACA_ENV`, `ALPACA_API_KEY`, `ALPACA_API_SECRET`, `ALPACA_LIVE_CONFIRM`
- `EXECUTOR_SHADOW_MODE`
- `AGGRESSIVE_TRANCHE_PCT`, `INITIAL_CAPITAL`
- `EXECUTOR_WINDOW_START/END`, `EXECUTOR_TICK_MINUTES`
- `PLANNER_DIRECT_SUBMIT_THRESHOLD`

Forbidden proposals are surfaced in the TG report ("🚫 REJECTED"). Useful signal if it happens repeatedly — means prompt needs tightening.

### Applier output

```python
@dataclass
class ApplierResult:
    applied_low: list[ProposedChange]
    queued_high: list[ProposedChange]
    rejected_forbidden: list[ProposedChange]
    rejected_out_of_bounds: list[ProposedChange]
    rejected_malformed: list[dict]
```

Each category appears in the TG report.

---

## 6. Trigger prompt, output schema, TG report format

### Structured output schema

The agent produces this JSON shape and writes it to `.cache/proposed_changes.json`:

```python
@dataclass(frozen=True)
class ProposedChange:
    key: str                  # config key name (must be in allowlist)
    current_value: Any        # echo of the current value
    proposed_value: Any       # new value
    rationale: str            # one paragraph: why — must cite specific data
    detailed_plan: str        # one paragraph: what happens — concrete portfolio effect
    expected_effect: str      # short: e.g. "cuts losers 15% faster; minor whipsaw risk"
    risk_tier: Literal["low", "high"]   # agent pre-classifies; applier verifies
    confidence: float         # 0..1

@dataclass(frozen=True)
class QuantReview:
    date: str                 # ISO date
    portfolio_summary: str    # 1-2 sentences on current state
    macro_read: str           # "risk-on at +0.45; VIX benign at 17.7"
    reasoning_summary: str    # 2-3 sentence thesis for today's recommendations
    data_gaps: list[str]      # any signals that were stale/missing
    proposed_changes: list[ProposedChange]
    no_changes_reason: Optional[str]   # populated if proposed_changes=[]
```

The `no_changes_reason` field is required when `proposed_changes=[]`. Empty proposals without this field is a malformed review — applier rejects.

### Trigger prompt (stored with the trigger, the agent's persona + instructions)

```
You are a senior US-equity quant with 15+ years experience in systematic
trading. You are reviewing the day's portfolio close state for a $100K
two-tranche system (core $90K: dual-momentum ETF rotation + value/quality
stock screen + macro overlay; aggressive $10K: leveraged-ETF momentum).

Your job: once per trading day, 3 hours after close, produce a structured
review JSON with zero or more proposed parameter changes.

WORKFLOW (run these commands in order using your Bash tool):

  1. Bash: python3 /Users/zl/works/stock/scripts/quant_fetch_portfolio.py
     → parses Alpaca state, returns portfolio JSON

  2. Bash: python3 /Users/zl/works/stock/scripts/quant_fetch_externals.py
     → runs 5 external-signal fetchers in parallel, returns JSON

  3. Read /Users/zl/works/stock/.cache/strategy_overrides.json (may not exist)
     → current active overrides — don't re-propose them

  4. Think step by step. Produce a QuantReview per the schema below.

  5. Write the JSON to /Users/zl/works/stock/.cache/proposed_changes.json

  6. Bash: python3 /Users/zl/works/stock/scripts/quant_apply.py
     /Users/zl/works/stock/.cache/proposed_changes.json
     → applier classifies, applies/queues/rejects, writes TG notification

  7. Read /Users/zl/works/stock/.cache/telegram_notifications.json,
     /Users/zl/works/stock/.cache/strategy_overrides.json, and
     /Users/zl/works/stock/.cache/strategy_proposals.json. Confirm state,
     summarize your final decisions in your response.

RULES:

- Default to proposing NO changes. Only propose when a specific signal
  justifies it. An unsupported change is worse than no change.

- Low-risk allowlist (auto-applies if within bounds):
    WATCHLIST           (additions only, ≤100 total)
    NEWS_SHOCK_KEYWORDS (additions only, ≤30 total)
    STOP_LOSS_PCT       (within ±20% of current AND [0.04, 0.20])
    TRAILING_STOP_PCT   (within ±20% of current AND [0.06, 0.25])
    CASH_BUFFER_PCT     (within ±50% of current AND [0.02, 0.20])

- High-risk allowlist (queues for user approval):
    WATCHLIST removals; NEWS_SHOCK_KEYWORDS removals; MOMENTUM_TOP_N;
    ETF_ALLOCATION_PCT; STOCK_ALLOCATION_PCT; SCREEN_MIN_ROE;
    SCREEN_MAX_PE; SCREEN_MAX_DEBT_EQUITY; MOMENTUM_LOOKBACK_MONTHS;
    SAFE_HAVEN; out-of-bound changes to the low-risk keys.

- Forbidden (never propose):
    DAILY_MAX_ORDERS, DAILY_MAX_NOTIONAL, LARGE_ORDER_THRESHOLD,
    any CIRCUIT_BREAKERS key, EXECUTOR_SHADOW_MODE,
    AGGRESSIVE_TRANCHE_PCT, INITIAL_CAPITAL, any path or credential
    constant, EXECUTOR_WINDOW_START/END, EXECUTOR_TICK_MINUTES,
    PLANNER_DIRECT_SUBMIT_THRESHOLD.

- Every proposed change MUST cite a specific data signal (which of the
  five sources, which datum). Changes without citations are malformed.

- Acknowledge staleness. 13F is quarterly-lagged — treat as positioning
  context, not current holdings. Flag missing data in `data_gaps`.

- When uncertain, prefer "no changes" with a clear `no_changes_reason`.

SCHEMA: see ProposedChange / QuantReview definitions in
/Users/zl/works/stock/quant/schema.py.
```

### TG report format

The applier formats the TG message. Example:

```
📊 Daily Strategy Review — 2026-04-19 07:00 local (7pm ET close)

Portfolio: $100,250 equity (+0.25% vs prior close)
Macro read: risk-on at +0.45; VIX 17.7 (benign); SPY +0.3% on day.
External signals: 5/5 healthy. (Gaps: none.)

Summary: Macro firmly risk-on but 13F shows three of six tracked funds
trimmed tech in Q4. Reddit sentiment on NVDA spiked bearish last 24h.
Pelosi disclosed a TSLA buy 3 days ago. Recommendation: maintain equity
exposure, slightly tighten risk controls, add two names from institutional
accumulation list.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ AUTO-APPLIED (low-risk)

1. WATCHLIST += ["PLTR", "SMCI"]
   Why: Both show >15% weekly gains with heavy institutional buying —
   13F Q4 shows Druckenmiller added PLTR; SMCI appeared in two of our
   tracked ARK funds this week.
   Plan: Added to core screener candidate universe. No direct portfolio
   change until next rebalance picks either up in top-N.
   Effect: Widens stock-sleeve search space from 53 → 55 names.
   Confidence: 0.85

2. STOP_LOSS_PCT: 0.08 → 0.075
   Why: 10-day realized ATR on core ETF holdings compressed 30%; tighter
   stop matches the new vol regime.
   Plan: Next rebalance attaches stops at 7.5% below entry instead of 8%.
   Existing trailing stops unchanged until replaced on the position.
   Effect: ~15% earlier cut on future losers; slight whipsaw risk on
   intraday swings.
   Confidence: 0.70

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏳ NEEDS YOUR APPROVAL (high-risk)

3. [prop_2026-04-19_01] MOMENTUM_TOP_N: 4 → 3
   Why: Top-1 (XLK) shows 3× the momentum score of rank-4 (MTUM);
   concentration historically improves Sharpe in trending regimes.
   Macro score firmly risk-on at +0.45.
   Plan: At next rebalance (today 21:35 local), drop MTUM's ~$15K
   position. Redistribute proportionally to XLK, QQQ, IWM (~$5K each).
   Concentration in top-3 rises 60% → 75%.
   Effect: Higher upside if leaders keep leading; larger drawdown if
   top-3 correct together.
   Confidence: 0.65
   Expires: 2026-04-19 21:35 local
   Approve: /strategy-approve prop_2026-04-19_01
   Reject:  /strategy-reject  prop_2026-04-19_01

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚫 REJECTED (forbidden keys attempted by LLM)
(none today)
```

Every change — auto or queued — has rationale + plan + effect + confidence. Low-risk changes were already applied at 7 AM; queued changes silently expire at 21:35 if no approval.

---

## 7. Error handling

Do-nothing-is-the-safe-default throughout.

| Failure | Behavior |
|---|---|
| Alpaca unreachable | Skip review, TG alert. No changes. |
| 1 of 5 external fetchers fails | Continue, note in `data_gaps`. |
| All external fetchers fail | Skip review, TG alert. |
| `claude` trigger fails / agent crashes | CC logs; next day's trigger runs fresh. |
| Agent produces malformed JSON | Applier rejects, writes minimal TG alert. No changes. |
| Agent proposes forbidden key | Applier rejects, surfaces in `🚫 REJECTED` section. |
| Agent proposes out-of-band low-risk value | Applier rejects (does not clamp — clamping corrupts the reasoning chain). |
| `.cache/strategy_overrides.json` corrupted on disk | `config.py`'s loader logs warning, ignores file, uses defaults. |

**Invariant:** if the review can't complete cleanly, the trading system continues on yesterday's config. The review is enhancement-only.

---

## 8. Testing

- **Unit tests for each data fetcher** (mock HTTP responses — SEC EDGAR filing structure, CapitolTrades JSON, ARK CSV parsing, Reddit response shape, yfinance ETF holdings).
- **Unit tests for applier classification** — each allowlisted key tests low-tier / high-tier / out-of-bounds / forbidden paths; each rejection path produces the expected TG report section.
- **Unit tests for `config.py` override loader** — valid JSON / corrupt JSON / unknown key / type mismatch / out-of-bounds. Each invalid case leaves defaults intact.
- **Unit test for CLI scripts** (`scripts/quant_fetch_*.py`, `scripts/quant_apply.py`) — invoke via subprocess, verify stdout shape and exit code.
- **Integration test: agent dry-run simulation** — with a canned `proposed_changes.json` (no agent invocation), verify end-to-end apply flow produces correct overrides + proposals + TG notification.
- **Opt-in integration test with real Claude Code trigger** — verify a test trigger can be created, fires, and produces a valid review against paper-mode data. Runs only with `-m integration`.

---

## 9. Rollout

Three phases.

1. **Phase 0: Dry-run** (~1 week). Trigger is created with an explicit `DRY_RUN=True` flag in the prompt. Agent calls `quant_apply.py --dry-run` which writes to `.cache/quant_review_dry.json` instead of the live files. TG notification still sends so you see what it would have done. Inspect daily: is the LLM's reasoning sound? Are the proposals actually good? Costs + latency as expected?

2. **Phase 1: Live applier, live queue** (~2–4 weeks). Update the trigger prompt to remove `DRY_RUN`. Low-risk auto-applies. High-risk lands in `.cache/strategy_proposals.json`. Manually approve via direct JSON edit (or via TG bot if ready). Watch for: drift, unusual proposals, correlation between LLM changes and portfolio P&L.

3. **Phase 2: TG bot approval handlers** (separate repo). Build `/strategy-pending`, `/strategy-approve <id>`, `/strategy-reject <id>`, `/strategy-revert <key>` in the TG bot project. The approval loop closes without SSHing.

---

## 10. Repo boundaries

| File | Writer | Reader |
|---|---|---|
| `.cache/strategy_overrides.json` | quant_apply.py + TG bot on `/strategy-approve` | `config.py` |
| `.cache/strategy_proposals.json` | quant_apply.py (append) + TG bot (consume) | TG bot, quant_apply.py (dedup) |
| `.cache/telegram_notifications.json` | quant_apply.py + executor (on breaker trip) | TG bot |
| `.cache/proposed_changes.json` | agent (intermediate) | quant_apply.py |
| `.cache/quant_review_dry.json` | quant_apply.py (Phase 0 only) | user manually |
| `.cache/quant_review.log` | quant_apply.py (append audit) | user |

This repo stays self-contained: if the TG bot isn't built, high-risk proposals expire unapproved — the auto-apply path keeps working.

---

## 11. Operational notes

- **Subscription auth:** the trigger runs under your Claude Code subscription. No `ANTHROPIC_API_KEY` env var needed.
- **Trigger creation:** done once via the `schedule` skill (`schedule create`). Trigger prompt + cron are stored on Anthropic's side. Updates via `schedule update`.
- **Schedule:** `0 7 * * 2-6` in your local +08 timezone covers 7 PM ET Mon-Fri (= close + 3h) correctly.
- **Quota:** daily scheduled triggers consume your CC subscription's usage; verify plan allows this cadence.
- **Manual run:** the trigger can be triggered ad-hoc via `schedule run <trigger-id>` for testing or if a scheduled fire is missed.
- **Trigger prompt iteration:** changes to the prompt require updating the trigger (`schedule update`). Store the canonical prompt source in version control — e.g., `quant/trigger_prompt.md` — so diffs are reviewable.

---

## 12. Open questions resolved during brainstorm

| Question | Resolution |
|---|---|
| Autonomy level | D' — constrained auto-apply within allowlist |
| Review triggers TG report? | Yes, every change — rich format with rationale + plan + effect + confidence |
| Default behavior if user doesn't approve | Tier C — low-risk auto-apply, high-risk expire at next rebalance |
| Data sources | F — 13F + Reddit + popular ETFs + ARK + Congress |
| LLM | A — Claude Opus 4.7 via subscription (not API) |
| Execution mechanism | B — scheduled remote trigger, not Python cron |
| Repo split | TG bot handlers out of scope for this repo |
| Feedback loops | Prevented by no cross-day memory |
