# Design: Value+Quality screen with buy-timing monitor

**Date:** 2026-06-27
**Status:** Approved (design)
**Scope:** A new standalone screen that ranks a universe for cheap-but-improving
("low value, high potential") stocks, plus a buy-timing monitor that tells you
*when* each candidate is a confirmed entry. Report/alert only — never places
orders.

## Background

The existing `screener.py` is a pure growth+momentum screen (CANSLIM C+A on
EPS/revenue growth, RS percentile, VCP base detection). It has no valuation
dimension, so it surfaces strong, often-extended names — not cheap ones. This
adds the missing **value + quality** selection layer and a separate
**entry-timing** layer (selection vs timing are different problems: buy
"cheap and turning up," not "cheap and falling").

## Goals

- Rank a universe by a composite of **value**, **quality**, and (optional)
  **improving** fundamentals, after hard liquidity/quality gates.
- For each candidate, classify entry readiness as **BUY / WATCH / WAIT** with a
  suggested risk-defined initial stop.
- Run standalone (CLI), persist candidates across runs, optionally notify on a
  flip to BUY — all alert-only.

## Non-goals

- No order placement; no changes to `screener.py` / `rebalancer.py` / order layer.
- No new data provider — uses the existing yfinance `fetch_info` / `fetch_prices`.
- No analyst-estimate-revision factor (needs a data source yfinance lacks).

## Data source

yfinance `.info` via the existing `data.fetch_info(ticker)` (cached, fail-open),
plus `data.fetch_prices` / `fetch_ohlcv` for price/volume. Fields used (any may
be absent → that factor is skipped for the ticker, fail-open):

- Value: `freeCashflow`, `marketCap`, `forwardPE`, `enterpriseToEbitda`, `priceToBook`
- Quality: `returnOnEquity`, `debtToEquity`, `freeCashflow`
- Gates: `averageVolume`, `marketCap`, last price
- Improving (optional, reuses `fetch_fundamentals`): `eps_q_growth`, `revenue_growth`

## Architecture

New module `value_screen.py`, pure functions + a thin CLI. Two stages.

### Stage 1 — Value+Quality screen (`screen_value_quality`)

Input: list of tickers (universe). For each ticker:

1. **Gates (drop before ranking):**
   - liquidity: `avg_volume * price >= MIN_DOLLAR_VOLUME`
   - price floor: `price >= MIN_PRICE` (default $5)
   - market cap: `marketCap >= MIN_MARKET_CAP`
   - trap guard: `freeCashflow > 0` OR `returnOnEquity > 0`
2. **Raw factors:**
   - value: `fcf_yield = freeCashflow/marketCap`, `earnings_yield = 1/forwardPE`,
     `ev_ebitda_inv = 1/enterpriseToEbitda`, `bm = 1/priceToBook`
   - quality: `roe = returnOnEquity`, `inv_debt = 1/(1+debtToEquity)`
   - improving (optional): `eps_q_growth`, `revenue_growth`
3. **Cross-sectional z-score** each raw factor across the surviving universe
   (winsorize at ±3σ), average within each sub-group → `value_z`, `quality_z`,
   `improving_z`.
4. **Composite** = `W_VALUE*value_z + W_QUALITY*quality_z + W_IMPROVING*improving_z`
   (default weights 0.5 / 0.35 / 0.15; improving contributes 0 when absent).
5. Rank descending; return top-N rows with factor breakdown.

Output: list of dicts `{ticker, composite, value_z, quality_z, improving_z,
price, fcf_yield, roe, ...}`.

### Stage 2 — Buy-timing monitor (`timing_signal`)

Input: a ticker + its daily OHLCV (from `fetch_ohlcv`). Computes:

- `above_200dma` = price > MA200 (trend up)
- `above_50dma` = price > MA50
- `ma50_rising` = MA50 today > MA50 `TREND_LOOKBACK` days ago
- `pivot` = highest high over last `PIVOT_LOOKBACK` days (ex-today)
- `breakout` = price > pivot AND today's volume > `VOL_MULT` × avg volume
- `rs_up` = trailing `RS_LOOKBACK` return > 0 (proxy for RS turning up)
- `extended` = price > pivot × (1 + `MAX_EXTENSION`)

Classification:
- **BUY**: `above_200dma` AND `ma50_rising` AND `breakout` AND NOT `extended`
- **WATCH**: `above_200dma` AND (`above_50dma` OR `rs_up`) but no confirmed breakout
- **WAIT**: otherwise (downtrend intact)

Also returns `suggested_stop` = `min(pivot_low, MA200)` where `pivot_low` is the
lowest low over `PIVOT_LOOKBACK` — a risk-defined initial stop. Returns
`{status, suggested_stop, reasons: [...] }`. Insufficient history → WAIT + reason.

### CLI / persistence

`python3 value_screen.py [--tickers A,B,..] [--top N] [--watch] [--notify]`

- default universe: read existing watchlist file if present
  (`config.WATCHLIST`), else require `--tickers`.
- runs Stage 1, then Stage 2 for each surviving candidate, prints a table:
  `ticker | composite | value_z | quality_z | price | TIMING | stop`.
- persists candidates to `.cache/value_candidates.json` (ticker + composite +
  first_seen + last status). `--watch` re-scores timing for the saved list
  without re-running Stage 1.
- `--notify`: when a candidate's status transitions to **BUY**, append a
  Telegram notification via `notifications.append_notification`
  (`source="value_screen"`), only on the WATCH/WAIT→BUY transition (no spam).

### Config additions (`config.py`)

```python
VS_MIN_DOLLAR_VOLUME = 2_000_000   # ADV * price floor (liquidity gate)
VS_MIN_PRICE = 5.0
VS_MIN_MARKET_CAP = 300_000_000
VS_TOP_N = 20
VS_WEIGHTS = {"value": 0.5, "quality": 0.35, "improving": 0.15}
VS_PIVOT_LOOKBACK = 20
VS_TREND_LOOKBACK = 10
VS_RS_LOOKBACK = 63
VS_VOL_MULT = 1.5
VS_MAX_EXTENSION = 0.05
```

## Error handling

Fail-open everywhere (mirrors `screener._fundamental_ok`): a ticker with missing
`.info` fields is dropped from the factor it lacks; a ticker failing a gate is
excluded; a ticker with insufficient price history → WAIT. A yfinance error for
one ticker never aborts the run. Empty universe → empty result, no crash.

## Testing (TDD)

- **gates:** below-liquidity / below-price / below-cap / negative-FCF-and-ROE
  tickers are excluded; a clean ticker survives.
- **z-score + composite:** with a hand-built 3-ticker universe, the cheaper +
  higher-quality name ranks first; weights applied correctly; absent
  `improving` contributes 0 (no NaN propagation).
- **timing:** synthetic OHLCV → breakout-above-pivot-on-volume-in-uptrend = BUY;
  uptrend-no-breakout = WATCH; below-200dma = WAIT; extended = not BUY;
  `suggested_stop` below entry. Insufficient history = WAIT.
- **persistence/notify:** WATCH→BUY transition appends exactly one notification;
  BUY→BUY does not re-notify.
- **fail-open:** empty `.info` and empty universe return cleanly.

## Rollout / verification

1. Unit tests pass.
2. Dry CLI run over a small `--tickers` list prints ranked candidates + timing.
3. Update `README.md` (new module + CLI + config flags).
