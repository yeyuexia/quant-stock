# Design: Two-track Russell 3000 value screen + module refactor

**Date:** 2026-06-27
**Status:** Approved (design)
**Scope:** Replace the `value` ensemble strategy's universe and logic with a
staged, two-track screen over the Russell 3000, and decompose it into
focused, independently-testable modules. The CANSLIM `screener` strategy is
unchanged (keeps the current Wikipedia watchlist). The strategy output shape
and everything downstream (`strategies.py` contract → `investor_agent` →
`watchdog`) are unchanged.

## Background

`value_screen.py` today pulls `config.WATCHLIST` (the same ~1500-name pool the
CANSLIM screen uses) and ranks it with a single winsorized z-score composite.
The user wants the value track to instead source the **Russell 3000** (full
market cap, including unprofitable names) and select with an explicit
**two-track rule filter** — profitable companies and unprofitable-growth
companies judged for "cheapness" on different yardsticks.

Two realities shape the design: (1) there is no clean Wikipedia Russell-3000
list — the iShares **IWV holdings CSV** is the source, and it is geo-gated for
non-US; (2) fetching yfinance `.info` for ~3000 names daily is untenable, so the
screen must be **staged** (cheap price/volume pre-filter → fundamentals only on
the survivors).

## Goals

- Source a true Russell 3000 universe (IWV CSV), weekly-cached, fail-open.
- Two-track selection (profitable / unprofitable-growth) with tunable thresholds.
- A clean module structure: volatile I/O at the edges, pure strategy logic in the
  middle, exhaustively unit-tested without network.
- Keep the `value` strategy fail-open and timeout-bounded so it can never stall
  the daily watchdog. Unchanged output schema.

## Non-goals

- No change to the CANSLIM `screener` strategy or its universe.
- No change to `strategies.py` / `investor_agent` / `watchdog`.
- No attempt to compute metrics yfinance cannot supply (5Y sales-growth CAGR,
  share-dilution YoY, operating-margin *trend*) — approximated or skipped,
  documented below.

## Decisions (confirmed)

- **Universe:** true Russell 3000 via the iShares **IWV** holdings CSV.
- **Liquidity knob:** `VS_MIN_DOLLAR_VOLUME = 5_000_000` (≈ $5M/day, for ~$50k
  single-name clips).
- Metrics yfinance can't supply reliably are approximated/skipped, fail-open.
- All thresholds live in `config.VS_TRACK_A` / `config.VS_TRACK_B`.

## Module structure (single responsibility, testable in isolation)

```
discovery.py            (+ get_russell3000_tickers)   universe acquisition
value_fundamentals.py   (NEW)  Fundamentals + from_info()   normalization (yfinance quirks)
value_prefilter.py      (NEW)  Stage-0 price/volume/$-vol gate   bulk filter (no .info)
value_tracks.py         (NEW)  Track A / Track B rules + scoring  pure strategy logic
value_screen.py         (REWRITE → thin orchestrator)   wires the stages, run()
```

### `discovery.get_russell3000_tickers() -> list[str]`
The only place that knows the IWV CSV format + geo-header.
- Fetch `config.RUSSELL3000_IWV_URL` with a browser `User-Agent` (past the
  disclaimer). Skip the CSV preamble (lines before the `Ticker,Name,...` header),
  parse with pandas, keep rows where `Asset Class == 'Equity'`, take the `Ticker`
  column, strip blanks / `-` / cash & derivative rows.
- Weekly cache (168h TTL), reusing the existing `_cache_get/_cache_set`.
- **Fail-open:** any HTTP error / format drift / empty parse → return `[]` (the
  value strategy then writes no rows; the ensemble/agent/watchdog degrade
  gracefully). Logged at WARNING.

### `value_fundamentals.py`
```python
@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    market_cap: Optional[float]
    is_profitable: bool            # trailingEps>0 or netIncomeToCommon>0
    pe: Optional[float]            # trailingPE
    peg: Optional[float]           # pegRatio
    ev_ebitda: Optional[float]     # enterpriseToEbitda
    ps: Optional[float]            # priceToSalesTrailing12Months
    rev_growth: Optional[float]    # revenueGrowth (TTM YoY; proxy for 5Y)
    eps_growth: Optional[float]    # earningsGrowth (this-year proxy)
    gross_margin: Optional[float]  # grossMargins
    op_margin: Optional[float]     # operatingMargins
    debt_equity: Optional[float]   # debtToEquity (normalized: yfinance reports %, ÷100)
    current_ratio: Optional[float] # currentRatio
    fcf: Optional[float]           # freeCashflow
    total_cash: Optional[float]    # totalCash

def from_info(ticker: str, info: dict) -> Fundamentals: ...
```
The single module that knows yfinance key names and `None`/NaN handling. Pure,
no network. Note: yfinance reports `debtToEquity` as a percent (e.g. 80 = 0.8);
`from_info` normalizes to a ratio so the `< 1` threshold is meaningful.

### `value_prefilter.prefilter(tickers, *, price_fn=None, cfg=...) -> list[str]`
Stage 0 — no `.info`. `price_fn(tickers) -> {ticker: (last_price, avg_dollar_vol)}`
(defaults to a batched `data.fetch_ohlcv`-based implementation; injectable for
tests). Drops `price <= VS_MIN_PRICE` and `avg_dollar_vol < VS_MIN_DOLLAR_VOLUME`.
Returns survivors, capped at `VS_PREFILTER_MAX` (highest dollar-volume first).

### `value_tracks.py` (pure; zero I/O)
```python
def classify(f: Fundamentals, cfg) -> Optional[str]:   # 'A' | 'B' | None
def passes(f: Fundamentals, track: str, cfg) -> bool:
def score(f: Fundamentals, track: str) -> float:       # higher = more attractive
```
- `classify`: market-cap gate first (`>= VS_MIN_MARKET_CAP`); then
  `'A'` if `f.is_profitable` else `'B'`.
- `passes` applies the track's gates (below). **Fail-open per field:** a missing
  field does not reject on that one gate, but a name must have at least one
  cheapness, one growth, and one solvency signal present to qualify (guards
  against an all-`None` shell passing on emptiness).
- `score`: within-track attractiveness — Track A blends earnings-yield (1/PE),
  PEG (lower better), growth, margin; Track B blends 1/PS, rev-growth, gross
  margin, cash-runway. Used only to rank within a track.

**Track A — profitable (config.VS_TRACK_A):**
`peg < 1`, `pe < 20`, *(optional `ev_ebitda < 12`)*, `rev_growth > 0.15`,
`eps_growth > 0.10`, `gross_margin > 0.30`, `debt_equity < 1`,
`current_ratio > 1.5`, `fcf > 0`.

**Track B — unprofitable growth (config.VS_TRACK_B):**
`ps < 6`, `rev_growth > 0.25`, `gross_margin > 0.40`, `debt_equity < 1`,
`cash_runway_quarters > 6` (≈ `total_cash / quarterly_burn`, where
`quarterly_burn = max(0, -fcf)/4`; profitable-cashflow → runway ∞ → passes),
`dilution < 0.10` (skipped if unavailable).

### `value_screen.run(tickers=None) -> list[dict]` (thin orchestrator)
1. universe = `tickers or discovery.get_russell3000_tickers()`
2. survivors = `value_prefilter.prefilter(universe)`
3. fetch `data.fetch_info` for survivors in a `ThreadPoolExecutor(VS_FETCH_WORKERS)`
4. `f = value_fundamentals.from_info(...)`; `track = classify(f)`;
   keep where `track and passes(f, track)`
5. rank within each track by `score`; interleave A/B; cap at `VS_TOP_N`;
   assign `rank`
6. `strategies.write_strategy_result("value", rows)` with the unchanged
   `{ticker, score, rank, factors}` schema (`factors` includes `track` + the key
   metrics for the agent's prompt and the UI).

## Data-availability honesty

Available in `.info`: `pegRatio, trailingPE, enterpriseToEbitda,
priceToSalesTrailing12Months, revenueGrowth, earningsGrowth, grossMargins,
operatingMargins, debtToEquity, currentRatio, freeCashflow, marketCap,
totalCash, trailingEps, netIncomeToCommon`. **Approximated/skipped:** 5Y sales
growth → TTM `revenueGrowth`; cash-runway → `total_cash / quarterly_burn`;
share-dilution YoY and operating-margin *trend* → skipped (no reliable history).

## Performance / caching

Universe weekly-cached; `fetch_info` cached at `CACHE_TTL_HOURS`; batch prices
via yfinance cache; Stage-1 `.info` fetched concurrently (`VS_FETCH_WORKERS`).
Warm-cache runs are fast; the cold first run is slower, so the `value` strategy
is given a longer bound via `ENSEMBLE_STRATEGY_TIMEOUT_SEC` (raised to 240). It
runs in the pre-market daily watchdog (not latency-critical) and stays
fail-open + timeout-skipped, so it can never stall the watchdog.

## Config additions (`config.py`)

```python
VS_MIN_PRICE = 5.0
VS_MIN_MARKET_CAP = 300_000_000
VS_MIN_DOLLAR_VOLUME = 5_000_000        # ≈ $5M/day liquidity gate
VS_PREFILTER_MAX = 500                  # cap survivors sent to fundamentals
VS_FETCH_WORKERS = 12                   # concurrent .info fetches
RUSSELL3000_IWV_URL = "https://www.ishares.com/us/products/239726/ishares-russell-3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
VS_TRACK_A = {"peg_max":1.0,"pe_max":20.0,"ev_ebitda_max":12.0,"rev_growth_min":0.15,
              "eps_growth_min":0.10,"gross_margin_min":0.30,"debt_equity_max":1.0,
              "current_ratio_min":1.5}
VS_TRACK_B = {"ps_max":6.0,"rev_growth_min":0.25,"gross_margin_min":0.40,
              "debt_equity_max":1.0,"cash_runway_quarters_min":6,"max_dilution":0.10}
```
`ENSEMBLE_STRATEGY_TIMEOUT_SEC` raised 90 → 240. `VS_TOP_N` is **kept** (the
final cap on emitted rows). `VS_WEIGHTS` (the old z-score weights) is retired —
the composite is gone. `VS_MIN_DOLLAR_VOLUME` changes 2_000_000 → 5_000_000.

## Error handling

Fail-open throughout: IWV fetch failure → `[]`; a per-ticker `.info` failure →
that ticker dropped; a missing field → that gate passes (with the
≥1-of-each-signal guard); empty universe/survivors → empty result. No path
raises; the strategy timeout is the backstop.

## Testing (TDD)

- **discovery:** IWV parser on a CSV fixture extracts equity tickers, drops
  cash/derivative rows; garbage / HTTP error → `[]`.
- **value_fundamentals:** `from_info` maps keys, normalizes `debtToEquity` %→ratio,
  sets `is_profitable`, returns `None` for absent fields; empty `.info` → all-None
  fundamentals (not a crash).
- **value_prefilter:** drops cheap (`≤$5`) and illiquid (`<$5M/day`); caps at
  `VS_PREFILTER_MAX` by dollar-volume; injected `price_fn`.
- **value_tracks (pure, the core):** `classify` routes profitable→A, unprofitable→B,
  sub-cap→None; Track-A accept + each-gate reject; Track-B accept + each-gate
  reject incl. cash-runway; missing-field fail-open + the ≥1-of-each-signal guard;
  `score` orders a cheaper/higher-quality name first.
- **value_screen.run:** end-to-end with injected universe/price/info fns →
  correct rows, schema `{ticker,score,rank,factors}` with `track`, capped at
  `VS_TOP_N`; fail-open on empty universe.

## Rollout / verification

1. Unit tests pass.
2. Offline dry-run with a small injected universe → ranked two-track rows.
3. One live `value_screen.run()` (warm or bounded) → sane Russell-3000 picks;
   confirm `strategies/value.json` schema unchanged.
4. Update `README.md` + `docs/system_overview.html` + `docs/architecture.html`
   (value-screen detail flow) per the living-doc instruction.

## Build phases (for the implementation plan)

1. config knobs + `value_fundamentals.py` (+ tests).
2. `discovery.get_russell3000_tickers` (+ fixture test).
3. `value_prefilter.py` (+ tests).
4. `value_tracks.py` — the two-track rules + scoring (+ exhaustive tests).
5. `value_screen.py` rewrite (orchestrator) (+ e2e test) + retire z-score.
6. docs (README, system_overview, architecture detail flow) + dry-run.
