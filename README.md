# Quantitative Investment System

A Python quant trading system for a $100K US-equity portfolio. Generates target allocations from dual-momentum ETF rotation, a value+quality stock screen, and a FRED-based macro regime overlay, then submits orders to **Alpaca** (paper by default) through a safety-gated order layer. Two tranches: a $90K balanced core and a $10K leveraged-ETF aggressive sleeve.

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
             orders.py   ◄── watchdog.py (signal-driven exits)
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
| `broker.py` | Thin Alpaca SDK wrapper. Raises `BrokerError` on any API failure; returns plain dataclasses (no SDK objects leak out). |
| `orders.py` | Policy layer: `sync_state`, `reconcile_to_targets`, `execute_plan`, `submit_exit`, pending-queue helpers, `tag_position`. |
| `rebalancer.py` | Cron entry point. Builds target weights per tranche and runs them through `orders.py`. |
| `watchdog.py` | Daily monitor. Reads live state via `orders.sync_state`, verifies brackets are attached, routes macro-driven exits. |
| `run.py` | Read-only reporter: "what would the system do right now?" Does not mutate portfolio state. |
| `config.py` | All parameters, mode selection, watchlists, safety caps. |
| `momentum.py` / `screener.py` / `macro.py` / `sentiment.py` / `risk.py` / `backtest.py` | Signal & analytics modules. Unchanged by the Alpaca integration. |
| `tests/` | `pytest` unit tests + opt-in Alpaca paper integration tests. |

## Commands

```bash
# Trading (Alpaca; paper by default)
python3 rebalancer.py --tranche core              # core tranche, mode-specific cadence
python3 rebalancer.py --tranche aggressive        # aggressive tranche, 7-day cadence
python3 rebalancer.py --tranche core --dry-run    # plan without submitting
python3 rebalancer.py --tranche core --force      # bypass "is it rebalance day?" gate

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
python3 discovery.py             # scan market for new candidates
python3 discovery.py --trending  # trending tickers from Reddit + Yahoo
python3 discovery.py --update    # scan + auto-update watchlist in config.py

# Tests
python3 -m pytest                              # unit tests only (integration deselected)
ALPACA_API_KEY=... ALPACA_API_SECRET=... \
  python3 -m pytest -m integration             # opt-in paper integration tests
```

## Safety Rails

Every order goes through four checks in `orders.py`, in this order:

1. **HALT file** (`.cache/HALT`) — one-line kill-switch. If present, all order logic exits cleanly and logs skipped intents. `touch .cache/HALT` to pause, `rm .cache/HALT` to resume.
2. **Paper/live guard** — live trading requires **both** `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`. Any single-env typo keeps you on paper.
3. **Daily caps** — `DAILY_MAX_ORDERS` (default 20) and `DAILY_MAX_NOTIONAL` (default $25K) in `config.py`. Excess orders are deferred for the next day.
4. **Large-order approval** — orders ≥ `LARGE_ORDER_THRESHOLD` ($2K default) are queued to `pending_orders.json` instead of submitted. A Telegram bot (separate project) approves/rejects via `/pending`, `/approve <id>`, `/reject <id>`. Orders expire after `PENDING_ORDER_TTL_HOURS` (default 6).

All four checks apply uniformly to scheduled rebalances, stop-loss exits, and signal-driven macro exits. Nothing bypasses them.

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

**ETF Universe:** SPY, QQQ, IWM, MDY, VTV, VUG, MTUM, QUAL, XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLRE, TLT, IEF, SHY
**Growth mode adds:** TQQQ, SOXL, UPRO, TNA, TECL, LABU

### Value + Quality Stock Screen

Composite score across mega-cap / large-cap / small-mid-cap:
- Value (30%): lower P/E ranks higher
- Quality (30%): higher ROE ranks higher
- Momentum (25%): stronger 3-month return ranks higher
- Growth (15%): higher revenue growth ranks higher

### Macro Regime Overlay (FRED)

Six indicators scored from −1 (bearish) to +1 (bullish):

| Indicator | FRED Series | Signal |
|---|---|---|
| Yield Curve (10Y−2Y) | DGS10, DGS2 | Recession risk when inverted |
| Credit Spreads (BAA−AAA) | DBAA, DAAA | Financial stress |
| Unemployment + Sahm Rule | UNRATE | Labor market deterioration |
| Fed Funds Rate | FEDFUNDS | Monetary policy direction |
| Financial Conditions (NFCI) | NFCI | Tightening vs loosening |
| Market Breadth | SP500 | S&P 500 vs 200-day SMA |

The composite score adjusts equity allocation between 40%–100% of target. When the regime flips to contraction, the watchdog also exits the aggressive-tranche leveraged ETFs via `orders.submit_exit`.

## Portfolio Modes (Core Tranche)

Aggressive tranche is always leveraged-ETF-only, top-2, weekly rotation, 10% stop / 15% trail.

| Parameter | Conservative | Balanced | Growth |
|---|---|---|---|
| ETF / Stock split | 90% / 10% | 80% / 20% | 50% / 50% |
| Leveraged ETFs | No | No | Yes (3x) |
| Rebalance cadence | 30 days | 30 days | 14 days |
| Stop-loss | 6% | 8% | 12% |
| Trailing stop | 10% | 12% | 18% |
| Cash buffer | 10% | 5% | 3% |
| Top-N ETFs held | 3 | 4 | 3 |

Set mode via environment variable:
```bash
export PORTFOLIO_MODE=growth
```

## Automation (cron, weekdays 8:30 AM ET)

```bash
crontab -e
30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py   >> .cache/watchdog.log 2>&1
0  9 * * 1-5 cd /Users/zl/works/stock && python3 rebalancer.py --tranche core       >> .cache/rebalance.log 2>&1
0  9 * * 1   cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive >> .cache/rebalance.log 2>&1
```

`rebalancer.py` no-ops unless the cadence threshold is reached, so running daily is safe.

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
