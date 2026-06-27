# Design: investor agent — dossier-grounded, 3-stage due-diligence

**Date:** 2026-06-28
**Status:** Approved (design)
**Scope:** Replace the single near-blind agent pick with a **dossier-grounded,
three-stage pipeline** (triage → deep analyst → portfolio-manager decision), so
the agent does professional due-diligence before selecting the daily buy
candidates. ~3 LLM calls/day. The watchdog/downstream are unchanged
(picks stay backward-compatible).

## Background

`quant/agent/investor.py::select_candidates` currently merges the strategy lists
into a pool and asks the `claude` CLI for the top-N knowing only ticker +
strategies + rank/score — a near-blind pick. Open-source prior art
(ai-hedge-fund, FinGPT-Forecaster, TradingAgents — research summary in this
session) converges on two lessons: (1) **precompute every number in code and
inject a per-candidate dossier** (LLMs hallucinate financial facts but reason
well over supplied data); (2) **multi-stage beats a single pass** — a separate
analyst step + a portfolio-manager synthesis (and bull/bear contrast) measurably
improves decisions. A single call over all 20–40 candidates also **dilutes
attention** per name. Hence the staged design below.

## Decisions (confirmed)

- **3-stage pipeline:** triage all candidates → deep-dive a shortlist → PM picks
  the final N. ~3 LLM calls/day (the cost knob).
- **Dossier every pool candidate** (in code), **including news/sentiment**.
- Grounding: the LLM reasons only over supplied dossier numbers (no tool-calls /
  no live browsing by the model).
- One daily run, inside the fail-open pre-market watchdog.

## Module structure

```
quant/agent/dossier.py    (NEW)  pure dossier assembly (no I/O)
quant/agent/investor.py   (EXTEND)  fetch data → dossiers → triage → analyst → PM → picks
```

### `quant/agent/dossier.py` (pure — no network)
```python
def build_dossier(ticker, *, info: dict, ohlcv=None, spy_ohlcv=None, news=None) -> dict
def compact_line(dossier: dict) -> str    # one-line summary for the triage prompt
```
`build_dossier` assembles, from *already-fetched* inputs (fully unit-testable):
```
{ "ticker": str,
  "valuation":   {pe, peg, ev_ebitda, ps, fcf_yield},
  "quality":     {gross_margin, op_margin, roe, debt_equity, current_ratio, profitable},
  "growth":      {rev_growth, eps_growth},
  "price_action":{price, pct_from_52w_high, pct_from_52w_low,
                  pct_vs_50dma, pct_vs_200dma, rsi14, rel_strength_vs_spy_3m},
  "analyst":     {recommendation, target_upside_pct, num_analysts},
  "insider":     {pct_held_insiders},
  "news":        {count, sentiment_score, sentiment_label, headlines: [<=3]} }
```
Every field **fail-open** (`None` when input missing). Pure helpers `_rsi(series,14)`,
`_pct_from(price, ref)`, `_rel_strength(tkr, spy)` are individually tested. Reuses
`value_fundamentals.from_info` (now `quant/data/fundamentals.py`) for the
fundamental fields. `compact_line` renders a dossier to a short triage row.

### `quant/agent/investor.py` — `select_candidates` (3-stage)
1. `pool = merge + dedupe + exclude owned` (unchanged).
2. **Fetch dossier inputs for the whole pool** concurrently
   (`ThreadPoolExecutor(AGENT_DOSSIER_WORKERS)`): `data.fetch_info`,
   `data.fetch_ohlcv` per ticker, `sentiment.fetch_yf_news` (when
   `AGENT_INCLUDE_NEWS`); `data.fetch_ohlcv("SPY")` once. Cached + fail-open.
   `dossiers = {t: build_dossier(t, …) for t in pool}`.
3. **Stage A — Triage (1 call).** Prompt = compact one-line dossier rows for the
   whole pool; ask for the `AGENT_SHORTLIST_N` (~8) most worth deep analysis as a
   JSON list of tickers. Parse/validate (subset of pool). **Fallback:** rule-rank
   (consensus + score) shortlist.
4. **Stage B — Deep analyst (1 call).** Prompt = the *full* dossiers for just the
   shortlist + an analyst persona that argues **bull case and bear case** per
   name from the supplied numbers; returns per-candidate
   `{ticker, signal: bullish|neutral|bearish, confidence 0-100, thesis, risks,
   catalysts, bull, bear}`. Parse/validate (tickers ⊆ shortlist). **Fallback:**
   synthesize neutral verdicts from the dossiers (deterministic).
5. **Stage C — PM decision (1 call).** Prompt = the analyst verdicts + a
   portfolio-manager persona; returns the final `picks` = top-`ENSEMBLE_TOP_N`
   tickers (⊆ shortlist) with a one-line portfolio rationale each.
   Parse/validate (exactly N, ⊆ shortlist). **Fallback:** top-N of the shortlist
   by analyst confidence, else rule-rank.
6. **Persist** `buy_candidates.json` picks, enriched additively:
   `{ticker, rationale, signal, confidence, thesis, risks, catalysts, strategies}`.
   The watchdog reads only `ticker` → backward-compatible.

All three stages share one injectable `llm_fn(prompt)->str|None` (the existing
`claude -p`); each prompt carries a distinct stage marker so a test fake can
branch. If *any* stage's LLM call/parse fails, that stage's deterministic
fallback runs and the pipeline continues — it never raises into the watchdog.

## Anti-hallucination (all stages)

Dossiers carry every number; prompts embed fixed reference thresholds (e.g.
"PE<20 cheap, rev_growth>15% strong, RSI>70 overbought, target_upside>20% rich")
and state: **"Use ONLY the numbers in each dossier; never invent a figure; null →
'unknown'."** Output is schema-only JSON; anything else → that stage's fallback.

## Performance / safety

Runs in the daily pre-market watchdog ensemble step (`_run_daily_ensemble`,
fail-open). Dossier inputs fetched concurrently + cached (`fetch_info` TTL,
yfinance price cache, sentiment 30-min cache). Each LLM call keeps the existing
120s subprocess timeout; ~3 calls/day total. Every failure degrades to a
deterministic fallback; nothing raises.

## Config additions (`config.py`)

```python
AGENT_DOSSIER_WORKERS = 12               # concurrent per-candidate data fetches
AGENT_INCLUDE_NEWS = True                # include news/sentiment in the dossier
AGENT_SHORTLIST_N = 8                    # triage output → deep-dive set
AGENT_RSI_PERIOD = 14
AGENT_REL_STRENGTH_LOOKBACK_DAYS = 63    # ~3 months vs SPY
# ENSEMBLE_TOP_N (=4) is the final PM pick count (existing).
```

## Error handling

Fail-open throughout: per-ticker fetch failure → that dossier's affected fields
`None`; empty pool → empty picks; triage/analyst/PM LLM failure / unparseable /
wrong tickers / wrong count → that stage's deterministic fallback. No path raises.

## Testing (TDD)

- **dossier (pure, the core):** `build_dossier` with injected info/ohlcv/spy/news
  → correct nested fields; missing inputs → `None`, no crash; `_rsi`, `_pct_from`,
  `_rel_strength` math on synthetic series; `compact_line` format; news section
  omitted when news=None.
- **triage:** valid LLM list → that shortlist (⊆ pool, ≤ N); bad/empty → rule-rank
  shortlist.
- **analyst:** valid verdict JSON over the shortlist → per-candidate fields incl.
  bull/bear; hallucinated ticker dropped; LLM None → deterministic neutral verdicts.
- **PM:** valid picks ⊆ shortlist, exactly ENSEMBLE_TOP_N → enriched picks;
  bad/None → top-N by confidence fallback.
- **end-to-end / orchestration:** injected fetchers + a stage-aware fake `llm_fn`
  → enriched `buy_candidates.json` with `ticker` (back-compat) + analysis fields;
  owned excluded; all-LLM-fail → rule-rank top-N (matches today's fallback shape).
- **regression:** existing `tests/test_investor_agent.py` select tests still pass.

## Rollout / verification

1. Unit tests pass; full suite green.
2. Offline dry-run: injected pool + fake info/ohlcv/news + a stage-aware fake
   `llm_fn` → enriched `buy_candidates.json` (triage→analyst→PM path exercised).
3. Docs: README agent section + `docs/system_overview.html` (investor card) +
   `docs/architecture.html` (`SUB.agent` detail flow) updated to the
   dossier → triage → analyst → PM pipeline.

## Build phases (for the implementation plan)

1. config knobs + `quant/agent/dossier.py` (pure: build_dossier + helpers + compact_line) + tests.
2. dossier-fetch orchestration in `investor.py` (concurrent pool fetch → dossiers) + tests.
3. Stage A/B/C prompts + parsers + per-stage fallbacks + enriched picks + tests.
4. docs + dry-run.
