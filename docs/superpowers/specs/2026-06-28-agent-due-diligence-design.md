# Design: enrich the investor agent with per-candidate due-diligence

**Date:** 2026-06-28
**Status:** Approved (design)
**Scope:** Before the daily agent picks the top-N buy candidates, give it a
**comprehensive, precomputed dossier** for each candidate and have it produce a
**structured professional verdict** per name, then select. One daily LLM call.
The watchdog/downstream are unchanged (picks stay backward-compatible).

## Background

`quant/agent/investor.py::select_candidates` currently merges the strategy
lists into a pool and asks the `claude` CLI for the top-N picks knowing only
each ticker + which strategies surfaced it + its rank/score. The pick is made
nearly blind. Open-source financial-agent prior art (ai-hedge-fund, FinGPT,
TradingAgents — see the research summary in this session) converges on a clear
pattern: **precompute every number in code, inject a per-candidate JSON dossier
with fixed thresholds, prompt an analyst persona, and force a constrained
output schema** — because LLMs hallucinate financial facts badly, but reason
well over supplied data.

## Goals

- A per-candidate dossier (valuation, quality/solvency, growth, price-action /
  technicals, analyst, insider, news/sentiment) built **in code** from data the
  repo already has (no new provider).
- A single daily LLM call that returns a **structured professional verdict per
  candidate** (signal, confidence, thesis, risks, catalysts) and the final picks.
- Grounded against hallucination; fail-open + bounded so it can't stall the
  daily watchdog; rule-rank fallback preserved; output schema additive.

## Decisions (confirmed)

- **Include news/sentiment** in each dossier (via existing `sentiment` module).
- **Dossier every pool candidate** (not a capped subset).
- One LLM call per day (unchanged cadence).

## Module structure

```
quant/agent/dossier.py    (NEW)  pure dossier assembly (no I/O)
quant/agent/investor.py   (EXTEND)  fetch data → dossiers → richer prompt → structured verdicts → picks
```

### `quant/agent/dossier.py` (pure — no network)
```python
def build_dossier(ticker, *, info: dict, ohlcv=None, spy_ohlcv=None, news=None) -> dict
```
Assembles, from *already-fetched* inputs (so it's fully unit-testable):
```
{
 "ticker": str,
 "valuation":  {pe, peg, ev_ebitda, ps, fcf_yield},
 "quality":    {gross_margin, op_margin, roe, debt_equity, current_ratio, profitable},
 "growth":     {rev_growth, eps_growth},
 "price_action": {price, pct_from_52w_high, pct_from_52w_low,
                  pct_vs_50dma, pct_vs_200dma, rsi14, rel_strength_vs_spy_3m},
 "analyst":    {recommendation, target_upside_pct, num_analysts},
 "insider":    {pct_held_insiders},
 "news":       {count, sentiment_score, sentiment_label, headlines: [<=3]},
}
```
Every field is **fail-open** (`None` when the input is missing). Helpers
`_rsi(series, 14)`, `_pct_from(price, ref)`, `_rel_strength(tkr_series, spy_series)`
are pure and individually tested. Reuses `value_fundamentals.from_info` for the
fundamental fields rather than re-reading `.info` keys.

### `quant/agent/investor.py` (extend `select_candidates`)
New flow inside the existing function:
1. `pool = merge + dedupe + exclude owned` (unchanged).
2. **Fetch dossier inputs for the whole pool** concurrently
   (`ThreadPoolExecutor(AGENT_DOSSIER_WORKERS)`): `data.fetch_info`,
   `data.fetch_ohlcv` (per ticker), `sentiment.fetch_yf_news` (news);
   `data.fetch_ohlcv("SPY")` once for relative strength. All cached + fail-open.
3. `dossiers = [dossier.build_dossier(t, info=…, ohlcv=…, spy_ohlcv=…, news=…) for t in pool]`.
4. **Prompt** (`_build_dossier_prompt`): an equity-analyst persona + the dossiers
   as JSON + explicit decision rules; instruction to use **only** supplied
   numbers and emit STRICT JSON.
5. **LLM call** (the existing one daily `claude -p`).
6. **Parse + validate** (`_parse_verdicts`): per-candidate
   `{ticker, signal: bullish|neutral|bearish, confidence: 0-100, thesis,
   risks, catalysts}` + `picks` (exactly top-N tickers from the pool). Invalid /
   missing → **rule-rank fallback** (consensus + score), as today.
7. **Persist** `buy_candidates.json` picks, each enriched additively:
   `{ticker, rationale (=thesis), signal, confidence, thesis, risks, catalysts,
   strategies}`. The watchdog reads only `ticker`, so this is backward-compatible.

## Prompt / anti-hallucination

The persona enumerates positives and concerns per candidate from the dossier,
then gives a verdict. The prompt embeds fixed reference thresholds (e.g. "PE<20
cheap, rev_growth>15% strong, RSI>70 overbought") and states: **"Use ONLY the
numbers in each dossier; never invent a figure; if a field is null, say
'unknown'."** Output is schema-only JSON; anything else → fallback.

## Performance / safety

Runs in the daily pre-market watchdog ensemble step (`_run_daily_ensemble`,
fail-open). Dossier inputs are fetched concurrently and cached (`fetch_info`
TTL, yfinance price cache, sentiment 30-min cache). The LLM call keeps its
existing 120s subprocess timeout. Any failure (fetch, LLM, parse) degrades to
the rule-rank fallback; nothing raises into the watchdog.

## Config additions (`config.py`)

```python
AGENT_DOSSIER_WORKERS = 12          # concurrent per-candidate data fetches
AGENT_INCLUDE_NEWS = True           # include news/sentiment in the dossier
AGENT_RSI_PERIOD = 14
AGENT_REL_STRENGTH_LOOKBACK_DAYS = 63   # ~3 months vs SPY
```

## Error handling

Fail-open throughout: a per-ticker fetch failure → that dossier's affected
fields are `None`; an empty pool → empty picks; an LLM failure / unparseable
output / wrong ticker / wrong count → rule-rank fallback. No path raises.

## Testing (TDD)

- **dossier (pure, the core):** `build_dossier` with injected info/ohlcv/spy/news
  → correct nested fields; missing inputs → `None` fields, no crash; `_rsi`,
  `_pct_from`, `_rel_strength` math on synthetic series; news section formats
  count/score/headlines and is omitted/empty when `AGENT_INCLUDE_NEWS` off.
- **investor orchestration:** with injected fetchers, dossiers built for the
  whole pool; the prompt contains the dossiers; valid LLM verdict JSON →
  enriched picks with `{signal,confidence,thesis,risks,catalysts}`; hallucinated
  ticker / missing picks / LLM None → rule-rank fallback; owned excluded;
  `buy_candidates.json` picks include `ticker` (back-compat) + the new fields.
- **regression:** existing `tests/test_investor_agent.py` select tests still pass
  (fallback path unchanged in shape).

## Rollout / verification

1. Unit tests pass; full suite green.
2. Offline dry-run: injected pool + fake info/ohlcv/news + a fake `llm_fn`
   returning verdict JSON → enriched `buy_candidates.json`.
3. Docs: README agent section + `docs/system_overview.html` (investor card) +
   `docs/architecture.html` (agent `SUB.agent` detail flow) updated to show the
   dossier → analyst-verdict → pick pipeline.

## Build phases (for the implementation plan)

1. config knobs + `quant/agent/dossier.py` (pure) + tests.
2. `investor.py` dossier orchestration (fetch pool data concurrently) + tests.
3. richer prompt + structured verdict parse + enriched picks + fallback + tests.
4. docs + dry-run.
