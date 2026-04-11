"""
Portfolio configuration for a $5,000 quantitative investment system.

Modes:
  conservative — capital preservation, ETF-heavy, wide stops
  balanced     — default, mix of ETFs and stocks
  growth       — aggressive, small/mid-cap heavy, leveraged ETFs, tighter rotation
"""
import os

# ── Portfolio Mode ──────────────────────────────────────────────
# Set via: PORTFOLIO_MODE=growth python3 run.py
# Or change the default here
PORTFOLIO_MODE = os.environ.get("PORTFOLIO_MODE", "balanced")

# ── Capital ─────────────────────────────────────────────────────
INITIAL_CAPITAL = 5000

# ── Mode-specific parameters ────────────────────────────────────
_MODE_PARAMS = {
    "conservative": {
        "max_position_pct": 0.20,
        "max_positions": 10,
        "cash_buffer_pct": 0.10,
        "stop_loss_pct": 0.06,
        "trailing_stop_pct": 0.10,
        "etf_allocation_pct": 0.90,      # 90% ETFs, 10% stocks
        "stock_allocation_pct": 0.10,
        "momentum_top_n": 3,
        "rebalance_days": 30,
        "use_leveraged_etfs": False,
    },
    "balanced": {
        "max_position_pct": 0.25,
        "max_positions": 8,
        "cash_buffer_pct": 0.05,
        "stop_loss_pct": 0.08,
        "trailing_stop_pct": 0.12,
        "etf_allocation_pct": 0.80,      # 80% ETFs, 20% stocks
        "stock_allocation_pct": 0.20,
        "momentum_top_n": 4,
        "rebalance_days": 30,
        "use_leveraged_etfs": False,
    },
    "growth": {
        "max_position_pct": 0.30,
        "max_positions": 8,
        "cash_buffer_pct": 0.03,
        "stop_loss_pct": 0.12,           # wider stops for volatile names
        "trailing_stop_pct": 0.18,
        "etf_allocation_pct": 0.50,      # 50% ETFs, 50% stocks
        "stock_allocation_pct": 0.50,
        "momentum_top_n": 3,             # concentrated bets
        "rebalance_days": 14,            # bi-weekly rebalance
        "use_leveraged_etfs": True,      # allow TQQQ, SOXL, etc.
    },
}

_params = _MODE_PARAMS.get(PORTFOLIO_MODE, _MODE_PARAMS["balanced"])

MAX_POSITION_PCT = _params["max_position_pct"]
MAX_POSITIONS = _params["max_positions"]
CASH_BUFFER_PCT = _params["cash_buffer_pct"]
STOP_LOSS_PCT = _params["stop_loss_pct"]
TRAILING_STOP_PCT = _params["trailing_stop_pct"]
ETF_ALLOCATION_PCT = _params["etf_allocation_pct"]
STOCK_ALLOCATION_PCT = _params["stock_allocation_pct"]
USE_LEVERAGED_ETFS = _params["use_leveraged_etfs"]

# ── Strategy 1: Dual Momentum ETF Rotation ──────────────────────
# Concept: Hold the top-N momentum ETFs; flee to safety (BIL/SHY) when
# all are below their moving average or absolute momentum is negative.

_ETF_BASE = [
    # US Broad Market
    "SPY",   # S&P 500
    "QQQ",   # Nasdaq-100
    "IWM",   # Russell 2000
    "MDY",   # S&P MidCap 400
    # US Style Factors
    "VTV",   # Value
    "VUG",   # Growth
    "MTUM",  # Momentum factor
    "QUAL",  # Quality factor
    # US Sectors
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Healthcare
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLRE",  # Real Estate
    # US Fixed Income (risk-off rotation targets)
    "TLT",   # Long-term treasury
    "IEF",   # 7-10yr treasury
    "SHY",   # Short-term treasury
]

_ETF_LEVERAGED = [
    # Leveraged (growth mode only — 2x/3x daily, high risk)
    "TQQQ",  # 3x Nasdaq-100
    "SOXL",  # 3x Semiconductors
    "UPRO",  # 3x S&P 500
    "TNA",   # 3x Small-Cap
    "TECL",  # 3x Technology
    "LABU",  # 3x Biotech
]

ETF_UNIVERSE = _ETF_BASE + (_ETF_LEVERAGED if USE_LEVERAGED_ETFS else [])

SAFE_HAVEN = "BIL"               # T-bill ETF (cash equivalent)
MOMENTUM_LOOKBACK_MONTHS = [1, 3, 6, 12]
MOMENTUM_TOP_N = _params["momentum_top_n"]
SMA_FILTER_PERIOD = 200          # 200-day SMA trend filter

# ── Strategy 2: Value + Quality Stock Screen ────────────────────
# Small-cap value + quality for satellite allocation (20% of portfolio)

SCREEN_MIN_MARKET_CAP = 500e6    # $500M+
SCREEN_MAX_MARKET_CAP = 20e9     # <$20B
SCREEN_MAX_PE = 20
SCREEN_MIN_ROE = 0.12
SCREEN_MAX_DEBT_EQUITY = 1.5
SCREEN_TOP_N = 10                # screen top 10, pick 2-3

# ── Rebalancing ─────────────────────────────────────────────────
REBALANCE_FREQUENCY_DAYS = _params["rebalance_days"]
TRANSACTION_COST_BPS = 5         # ~$0.05 per $100 traded

# ── Candidate individual stocks for deeper analysis ─────────────
# High-conviction watchlist (updated by screening)
WATCHLIST = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    # Financials
    "BRK-B", "JPM", "V", "MA", "GS", "BAC",
    # Healthcare
    "UNH", "JNJ", "ABBV", "MRK", "LLY", "PFE",
    # Consumer
    "COST", "HD", "PG", "PEP", "KO", "WMT", "MCD",
    # Industrials & Energy
    "CAT", "GE", "UNP", "XOM", "CVX",
    # Semiconductors
    "AVGO", "AMD", "INTC", "QCOM",
    # ── Small & Mid-Cap Growth (higher volatility, higher upside) ──
    "CROX",  # Crocs — strong brand momentum, low P/E
    "DECK",  # Deckers (Hoka/UGG) — premium consumer growth
    "CAVA",  # Cava Group — fast-casual restaurant growth
    "DUOL",  # Duolingo — edtech, high revenue growth
    "AXON",  # Axon Enterprise — law enforcement tech monopoly
    "CELH",  # Celsius Holdings — energy drinks challenger
    "TOST",  # Toast — restaurant SaaS platform
    "APP",   # AppLovin — mobile ad tech, huge momentum
    "RKLB",  # Rocket Lab — space/defense small-cap
    "PLTR",  # Palantir — AI/government data, high buzz
    "SOFI",  # SoFi Technologies — fintech disruptor
    "HOOD",  # Robinhood — retail brokerage, cheap
    "AFRM",  # Affirm — buy-now-pay-later
    "NET",   # Cloudflare — cybersecurity/CDN growth
    "DKNG",  # DraftKings — online sports betting
    "SMCI",  # Super Micro Computer — AI infrastructure
    "IONQ",  # IonQ — quantum computing pure play
    "MARA",  # Marathon Digital — crypto/bitcoin proxy
    "LUNR",  # Intuitive Machines — space/lunar missions
    "RDDT",  # Reddit — social platform, recently IPO'd
]
