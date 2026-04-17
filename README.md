# Quantitative Investment System

A Python-based quantitative investment system for the US stock market. Designed for a $5,000 starting portfolio with three risk modes and automated daily monitoring.

## Quick Start

```bash
pip3 install -r requirements.txt

# Copy .env.example to .env and fill in:
#   FRED_API_KEY (free)                  https://fred.stlouisfed.org/docs/api/api_key.html
#   ALPACA_API_KEY + ALPACA_API_SECRET   https://app.alpaca.markets/paper/dashboard/overview
cp .env.example .env
# edit .env

# 1. See what the system would do (read-only)
python3 run.py

# 2. Dry-run the rebalancer (prints plan, submits nothing)
python3 rebalancer.py --tranche core --dry-run
python3 rebalancer.py --tranche aggressive --dry-run

# 3. When ready, let it place orders on the paper account
python3 rebalancer.py --tranche core
```

## Commands

```bash
# Trading (Alpaca; paper by default)
python3 rebalancer.py --tranche core              # core tranche, mode-specific cadence
python3 rebalancer.py --tranche aggressive        # aggressive tranche, weekly
python3 rebalancer.py --tranche core --dry-run    # plan without submitting
python3 rebalancer.py --tranche core --force      # bypass cadence gate

# Kill-switch
touch .cache/HALT                                  # pause all order logic
rm .cache/HALT                                     # resume

# Tag an externally-opened position so it's counted in a tranche
python3 -c "from orders import tag_position; tag_position('NVDA', 'core', 'manual 2026-04-17')"

# Read-only analysis (three modes)
python3 run.py                              # balanced (default)
PORTFOLIO_MODE=growth python3 run.py        # aggressive: leveraged ETFs + small-caps
PORTFOLIO_MODE=conservative python3 run.py  # capital preservation

# Daily monitoring
python3 watchdog.py              # full check: prices, stops, macro, news
python3 watchdog.py --quick      # price moves + stop-loss only
python3 watchdog.py --portfolio  # current positions and P&L
python3 watchdog.py --history    # portfolio value over time

# Stock discovery
python3 discovery.py             # scan market for new candidates
python3 discovery.py --trending  # trending tickers from Reddit + Yahoo
python3 discovery.py --update    # scan + auto-update watchlist in config.py
```

## System Architecture

```
run.py              Main entry point — ties all modules together
config.py           Portfolio parameters, mode selection, watchlists
data.py             Market data fetching + caching (yfinance)
momentum.py         Dual momentum ETF rotation strategy
screener.py         Value + quality + growth stock screener
macro.py            FRED-based macro regime detection (6 indicators)
sentiment.py        News (Yahoo Finance) + Reddit sentiment monitor
risk.py             Portfolio analytics: Sharpe, VaR, drawdown, Kelly
backtest.py         Historical backtesting engine
watchdog.py         Daily alerts: stop-loss, volume spikes, regime shifts
discovery.py        Auto stock discovery from S&P 500, Yahoo, Reddit
```

## Strategies

### Strategy 1: Dual Momentum ETF Rotation

Ranks 19 US ETFs (+ 6 leveraged in growth mode) by a composite momentum score blending 1/3/6/12-month returns. Applies a 200-day SMA filter for absolute momentum. Holds the top N ETFs equal-weighted; rotates to T-bills (BIL) when all signals turn negative.

Based on Gary Antonacci's dual momentum research (2014).

**ETF Universe:** SPY, QQQ, IWM, MDY, VTV, VUG, MTUM, QUAL, XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLRE, TLT, IEF, SHY

**Growth mode adds:** TQQQ (3x Nasdaq), SOXL (3x Semis), UPRO (3x S&P), TNA (3x Small-Cap), TECL (3x Tech), LABU (3x Biotech)

### Strategy 2: Value + Quality Stock Screen

Screens 55 US stocks across mega-cap, large-cap, and small/mid-cap for a composite score of:
- Value (30%): lower P/E ranks higher
- Quality (30%): higher ROE ranks higher
- Momentum (25%): stronger 3-month return ranks higher
- Growth (15%): higher revenue growth ranks higher

### Macro Regime Overlay

Six FRED indicators scored from -1 (bearish) to +1 (bullish):

| Indicator | FRED Series | What It Signals |
|-----------|-------------|-----------------|
| Yield Curve (10Y-2Y) | DGS10, DGS2 | Recession risk (inverted = danger) |
| Credit Spreads (BAA-AAA) | DBAA, DAAA | Financial stress |
| Unemployment + Sahm Rule | UNRATE | Labor market deterioration |
| Fed Funds Rate | FEDFUNDS | Monetary policy direction |
| Financial Conditions (NFCI) | NFCI | Tightening vs loosening |
| Market Breadth | SP500 | S&P 500 vs 200-day SMA |

The composite score adjusts equity allocation between 40%-100% of target. When the regime shifts to contraction, the system forces more capital into safety (BIL/cash).

### News & Social Sentiment

Monitors Yahoo Finance news and Reddit (r/wallstreetbets, r/stocks, r/investing, r/stockmarket) for:
- Trending ticker mentions weighted by engagement
- Keyword-based sentiment scoring (bullish/bearish/neutral)
- Portfolio-specific alerts when holdings appear in news
- Overall market mood indicator

## Portfolio Modes

| Parameter | Conservative | Balanced | Growth |
|-----------|-------------|----------|--------|
| ETF / Stock split | 90% / 10% | 80% / 20% | 50% / 50% |
| Leveraged ETFs | No | No | Yes (3x) |
| Rebalance cycle | 30 days | 30 days | 14 days |
| Stop-loss | 6% | 8% | 12% |
| Trailing stop | 10% | 12% | 18% |
| Cash buffer | 10% | 5% | 3% |
| Top N ETFs held | 3 | 4 | 3 |

Set mode via environment variable:
```bash
export PORTFOLIO_MODE=growth
```

## Daily Watchdog

The watchdog (`watchdog.py`) checks for:

- **Price alerts** — holdings moving >3% in a day
- **Stop-loss triggers** — position falls below entry by stop-loss %
- **Trailing stop** — position falls from its peak by trailing stop %
- **Volume anomalies** — trading volume >2x the 20-day average
- **Macro regime shifts** — composite score or regime label changes
- **Sahm Rule** — unemployment trigger crossing 0.50 (recession signal)
- **News/sentiment** — bearish headlines on held positions
- **Rebalance reminder** — days since last rebalance exceeds threshold

Portfolio value is logged daily to `daily_log.csv` for tracking.

### Automate with cron (weekdays 8:30 AM ET):
```bash
crontab -e
30 8 * * 1-5 cd /Users/zl/works/stock && python3 watchdog.py >> .cache/watchdog.log 2>&1
0  9 * * 1-5 cd /Users/zl/works/stock && python3 rebalancer.py --tranche core >> .cache/rebalance.log 2>&1
0  9 * * 1   cd /Users/zl/works/stock && python3 rebalancer.py --tranche aggressive >> .cache/rebalance.log 2>&1
```

Note: rebalancer.py no-ops unless cadence is reached, so running daily is fine.

## Auto Stock Discovery

`discovery.py` scans multiple sources for new investment candidates:

1. **S&P 500** — random sample of index components
2. **Yahoo Finance** — most active, daily gainers
3. **Reddit** — trending ticker mentions weighted by upvotes/comments
4. **Current watchlist** — re-screens existing picks

Each candidate is scored on revenue growth, ROE, momentum, valuation, and trend. Results are categorized into growth, value, small/mid-cap momentum, and quality dividend buckets.

Run `python3 discovery.py --update` to automatically add top discoveries to the watchlist.

## Risk Management

- **Position sizing**: no single position exceeds max position % of portfolio
- **Stop-losses**: hard stop at entry price minus stop-loss %, trailing stop from peak
- **Macro overlay**: reduces equity exposure in deteriorating macro conditions
- **Diversification**: correlation matrix monitoring, diversification ratio tracking
- **Metrics**: annualized Sharpe ratio, max drawdown, VaR/CVaR at 95%, win rate

## Safety Rails

Every order — rebalance, stop-exit, or signal-driven — goes through `orders.py`:

1. **HALT file** (`.cache/HALT`) — if present, all order logic exits cleanly.
2. **Paper/live guard** — live mode requires both `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`.
3. **Daily caps** — `DAILY_MAX_ORDERS` and `DAILY_MAX_NOTIONAL` in `config.py`.
4. **Large-order approval** — orders ≥ `LARGE_ORDER_THRESHOLD` ($2K default) go to `pending_orders.json` and require Telegram approval before submission.

## Switching to live

Paper is the default. Before flipping to live:

1. Run on paper for several weeks. Review `daily_log.csv`, verify brackets always attach, watch for Telegram prompts that were wrong.
2. Set `DAILY_MAX_NOTIONAL` to a small number (e.g. $500) in `config.py`.
3. Export `ALPACA_ENV=live` and `ALPACA_LIVE_CONFIRM=yes`.
4. Ramp `DAILY_MAX_NOTIONAL` up over subsequent weeks.

## Data Sources

| Source | Cost | What |
|--------|------|------|
| yfinance | Free | Price data, fundamentals, news |
| FRED API | Free | Macro indicators (requires free API key) |
| Reddit JSON | Free | Social sentiment from finance subreddits |
| Wikipedia | Free | S&P 500 component list |

## File Structure

```
stock/
  run.py            Main entry point
  config.py         All parameters and watchlists
  data.py           Data fetching + caching
  momentum.py       ETF momentum rotation
  screener.py       Stock screening
  macro.py          FRED macro regime
  sentiment.py      News + Reddit sentiment
  risk.py           Portfolio risk analytics
  backtest.py       Strategy backtesting
  watchdog.py       Daily monitoring + alerts
  discovery.py      Auto stock discovery
  requirements.txt  Python dependencies
  .env              API keys (not committed)
  .cache/           Cached market data (not committed)
  portfolio.json    Tracked positions (not committed)
  daily_log.csv     Daily P&L log (not committed)
```

## Requirements

- Python 3.7+
- Dependencies: `yfinance`, `pandas`, `numpy`, `scipy`, `tabulate`, `fredapi`
