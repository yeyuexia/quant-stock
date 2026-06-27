# Design: investor agent — dossier-grounded, bias-controlled due-diligence

**Date:** 2026-06-28
**Status:** Approved (design)
**Scope:** Replace the near-blind daily agent pick with a **peer-relative,
dossier-grounded, bias-controlled pipeline**: balanced shortlist → batched
analyst (bull/bear) → critic/verification → portfolio-manager decision with
abstention. The watchdog/downstream are unchanged (picks stay
backward-compatible). ~3 LLM calls/day.

## Background

`quant/agent/investor.py::select_candidates` currently merges the two strategy
lists (CANSLIM growth/momentum + two-track value) into a pool and asks the
`claude` CLI for the top-N knowing only ticker + which strategies surfaced it +
rank/score — a near-blind pick. Open-source prior art (ai-hedge-fund,
FinGPT-Forecaster, TradingAgents — research summary in this session) gives three
lessons baked into this design: (1) **precompute every number in code and inject
a per-candidate dossier** (LLMs hallucinate financial facts but reason well over
supplied data); (2) **multi-stage beats one pass** (a separate analyst step + a
verification/critic step measurably improve decisions); (3) **judge each name
relative to its peers**, not on one absolute yardstick.

**Source-bias risk (explicit goal):** the two sources have opposite styles —
value names look cheap (low PE/PS), CANSLIM names look expensive-but-growing
(high PE, high RS). Judged on one absolute scale, the agent systematically
favors one style. The merged pool also barely overlaps, so "consensus" is
meaningless across sources. This design neutralizes that with **peer-relative
metrics + a balanced, blind shortlist + source-mix monitoring**.

## Decisions (confirmed)

- **Dossier every pool candidate in code**, including **news/sentiment**,
  **peer-relative (sector) z-scores**, and **estimate revisions + earnings
  surprises**.
- **De-bias:** balanced shortlist (each source contributes its top-K),
  **blind** analysis (source labels hidden from the LLM), and **source-mix
  logging** of the final picks. No hard quota (it would force weak buys and
  conflict with abstention).
- **Pipeline:** deterministic balanced shortlist → analyst (batched, bull/bear)
  → critic/verification → PM. ~3 LLM calls/day.
- **Abstention:** PM returns **0–5** picks; only names clearing a conviction
  floor are bought ("cash is a position").
- One daily run, inside the fail-open pre-market watchdog.

## Module structure

```
quant/agent/dossier.py    (NEW)  pure dossier assembly + peer-relative z-scores (no I/O)
quant/agent/investor.py   (EXTEND)  fetch → dossiers → balanced shortlist → analyst → critic → PM → picks
```

### `quant/agent/dossier.py` (pure — no network)
```python
def build_dossier(ticker, *, info, ohlcv=None, spy_ohlcv=None, news=None, estimates=None) -> dict
def add_peer_relative(dossiers: list[dict], *, min_group: int) -> None   # in-place sector z-scores
def compact_line(dossier: dict) -> str
```
`build_dossier` assembles, from *already-fetched* inputs (fully unit-testable):
```
{ "ticker": str, "sector": str|None,
  "valuation":   {pe, peg, ev_ebitda, ps, fcf_yield},
  "quality":     {gross_margin, op_margin, roe, debt_equity, current_ratio, profitable},
  "growth":      {rev_growth, eps_growth},
  "estimates":   {revision_trend: "rising"|"falling"|"flat"|None,
                  up_revisions_90d, down_revisions_90d, surprises: [<=4 floats]},
  "price_action":{price, pct_from_52w_high, pct_from_52w_low,
                  pct_vs_50dma, pct_vs_200dma, rsi14, rel_strength_vs_spy_3m},
  "analyst":     {recommendation, target_upside_pct, num_analysts},
  "insider":     {pct_held_insiders},
  "news":        {count, sentiment_score, sentiment_label, headlines: [<=3]},
  "peer_relative": {pe_z, ps_z, ev_ebitda_z, rev_growth_z, gross_margin_z}  # filled by add_peer_relative
}
```
Every field **fail-open** (`None` when input missing). Pure helpers `_rsi(series,14)`,
`_pct_from(price, ref)`, `_rel_strength(tkr, spy)`, `_zscore(values)` are tested
individually. Reuses `quant/data/fundamentals.py::from_info` for the fundamental
fields. **`add_peer_relative`** groups the candidate set by `sector` and, where a
sector has ≥ `min_group` members, writes the sector-relative z-score of each
metric (lower-is-better metrics — PE/PS/EV-EBITDA — negated so "+z = better");
sectors below the threshold fall back to pool-wide z-score; absolute values are
always kept alongside. This is what makes "cheap" mean *cheap-for-its-industry*
and equalizes value vs growth comparison.

### `quant/agent/investor.py` — `select_candidates`
1. `pool = merge + dedupe + exclude owned` (unchanged); keep an internal
   `source_of[ticker]` map (which strategy/strategies surfaced it).
2. **Fetch dossier inputs for the whole pool** concurrently
   (`ThreadPoolExecutor(AGENT_DOSSIER_WORKERS)`): `data.fetch_info`,
   `data.fetch_ohlcv` per ticker, `data.fetch_estimates` (NEW: revisions +
   surprises, fail-open), `sentiment.fetch_yf_news` (when `AGENT_INCLUDE_NEWS`);
   `data.fetch_ohlcv("SPY")` once. All cached + fail-open. Build dossiers, then
   `dossier.add_peer_relative(dossiers, min_group=AGENT_PEER_MIN_GROUP)`.
3. **Stage A — Balanced shortlist (deterministic, no LLM).** From each source
   take its top `AGENT_SHORTLIST_PER_SOURCE` by that strategy's own rank; union +
   dedupe → the shortlist (≤ `AGENT_SHORTLIST_N`). Guarantees both styles are
   represented and is the de-bias backbone. Source labels are **not** carried
   into any prompt past this point.
4. **Stage B — Analyst (1 LLM call, source-blind, batched).** Prompt = the full
   dossiers (incl. peer-relative) for the shortlist + an analyst persona that
   argues **bull case and bear case** per name from the supplied numbers; returns
   per-candidate `{ticker, signal: bullish|neutral|bearish, confidence 0-100,
   thesis, risks, catalysts, bull, bear}`. Validate (tickers ⊆ shortlist).
   **Fallback:** deterministic neutral verdicts from the dossiers.
5. **Stage C — Critic / verification (1 LLM call).** Prompt = the analyst
   verdicts + their dossiers; the critic checks every claim against the dossier
   numbers, **strikes unsupported claims**, and **caps overstated confidence**;
   returns the adjusted verdicts (same schema + `critic_notes`). Validate.
   **Fallback:** pass the analyst verdicts through unchanged.
6. **Stage D — PM decision (1 LLM call, abstention).** Prompt = the critic-adjusted
   verdicts + a portfolio-manager persona + the rule **"buy only names with
   conviction ≥ `AGENT_CONVICTION_FLOOR`; return between 0 and `AGENT_MAX_PICKS`;
   prefer cash to a weak buy; aim for the best risk-adjusted set, not a fixed
   count."** Returns `picks` (0–5 tickers ⊆ shortlist) + one-line rationale each.
   Validate (≤ MAX_PICKS, ⊆ shortlist, each ≥ floor). **Fallback:** the
   shortlist names with confidence ≥ floor, capped at MAX_PICKS, by confidence.
7. **Persist + monitor.** Write `buy_candidates.json` picks, enriched additively:
   `{ticker, rationale, signal, confidence, thesis, risks, catalysts, strategies}`.
   Append one row to `.cache/agent_source_mix.csv`
   (`date, n_picks, n_value, n_canslim, n_other, shortlist_value, shortlist_canslim`)
   so source bias is **measurable over time**. The watchdog reads only `ticker`
   → backward-compatible.

All LLM stages share one injectable `llm_fn(prompt)->str|None` (the existing
`claude -p`); each prompt carries a distinct stage marker so a test fake can
branch. Any stage's LLM/parse failure → that stage's deterministic fallback; the
pipeline never raises into the watchdog. If *every* LLM stage fails, the result
equals today's rule-rank top-N (balanced) — no regression.

## Anti-hallucination (all LLM stages)

Dossiers carry every number; prompts embed fixed reference thresholds (e.g.
"PE<20 cheap, rev_growth>15% strong, RSI>70 overbought, target_upside>20% rich,
peer-z>+1 strong-vs-industry") and state: **"Use ONLY the numbers in each
dossier; never invent a figure; null → 'unknown'."** Output is schema-only JSON;
anything else → that stage's fallback. The critic stage is a second line of
defense specifically against unsupported claims and overconfidence.

## De-bias mechanics (summary)

- **Balanced shortlist** (Stage A): each source contributes its top-K → fair
  representation regardless of how many each source emits.
- **Blind judging** (Stages B–D): no source label reaches the LLM; peer-relative
  metrics let value and growth be compared on equal, within-style footing.
- **No hard quota:** final selection is merit + conviction (abstention allowed),
  so balance is in the *opportunity*, not forced into the *outcome*.
- **Monitoring:** `.cache/agent_source_mix.csv` records the source split of
  shortlist and picks daily, making any residual drift visible and tunable.

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
AGENT_SHORTLIST_PER_SOURCE = 4           # each source's top-K into the shortlist
AGENT_SHORTLIST_N = 8                    # shortlist cap (union of per-source tops)
AGENT_MAX_PICKS = 5                      # PM picks 0..5 (abstention)
AGENT_CONVICTION_FLOOR = 50              # min confidence (0-100) to buy a name
AGENT_PEER_MIN_GROUP = 3                 # min sector members to use sector z-score
AGENT_RSI_PERIOD = 14
AGENT_REL_STRENGTH_LOOKBACK_DAYS = 63    # ~3 months vs SPY
```

## Error handling

Fail-open throughout: per-ticker fetch failure → that dossier's affected fields
`None`; empty pool → empty picks; empty shortlist → empty picks; any LLM stage
failure / unparseable / wrong tickers / over-count → that stage's deterministic
fallback. No path raises. Source-mix logging is best-effort (a log failure never
blocks picks).

## Testing (TDD)

- **dossier (pure, the core):** `build_dossier` fields from injected
  info/ohlcv/spy/news/estimates; missing inputs → `None`, no crash; `_rsi`,
  `_pct_from`, `_rel_strength`, `_zscore` math on synthetic series;
  `add_peer_relative` — sector group ≥ min_group → sector z-scores (lower-better
  metrics negated), small sector → pool-wide fallback; `compact_line` format.
- **balanced shortlist:** pool with lopsided source counts (e.g. 15 value, 4
  canslim) → shortlist still draws each source's top-K (canslim not starved);
  dedupe across sources; cap at SHORTLIST_N.
- **analyst:** valid verdict JSON over the shortlist → fields incl. bull/bear;
  hallucinated ticker dropped; LLM None → deterministic neutral verdicts; prompt
  contains NO source label.
- **critic:** strikes a claim absent from the dossier / caps confidence; LLM None
  → analyst verdicts pass through unchanged.
- **PM / abstention:** all-weak shortlist (every confidence < floor) → **0 picks**;
  mixed → only ≥-floor names, capped at MAX_PICKS (≤5); over-count / bad → fallback.
- **monitoring:** a run writes one `.cache/agent_source_mix.csv` row with correct
  value/canslim counts for shortlist and picks.
- **end-to-end:** injected fetchers + a stage-aware fake `llm_fn` → enriched
  `buy_candidates.json` with `ticker` (back-compat) + analysis fields; owned
  excluded; all-LLM-fail → rule-rank balanced top-N.
- **regression:** existing `tests/test_investor_agent.py` select tests still pass.

## Rollout / verification

1. Unit tests pass; full suite green.
2. Offline dry-run: injected pool (lopsided sources) + fake
   info/ohlcv/news/estimates + a stage-aware fake `llm_fn` → enriched
   `buy_candidates.json` + a `agent_source_mix.csv` row (full pipeline exercised).
3. Docs: README agent section + `docs/system_overview.html` (investor card) +
   `docs/architecture.html` (`SUB.agent` detail flow) updated to the
   dossier → balanced-shortlist → analyst → critic → PM pipeline.

## Build phases (for the implementation plan)

1. config knobs + `quant/agent/dossier.py` (build_dossier + helpers + add_peer_relative + compact_line) + tests.
2. `data.fetch_estimates` (revisions + surprises, fail-open) + dossier-fetch orchestration in `investor.py` (concurrent pool fetch → dossiers → peer-relative) + balanced shortlist + tests.
3. Stage B analyst + Stage C critic + Stage D PM prompts/parsers + per-stage fallbacks + abstention + enriched picks + tests.
4. source-mix monitoring + docs + dry-run.
```
