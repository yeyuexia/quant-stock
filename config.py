"""
Portfolio configuration for a $100,000 quantitative investment system.

Capital structure (two-tranche):
  Core tranche    $90,000 (90%) — balanced mode: ETF rotation + stock screen
  Aggressive tranche $10,000 (10%) — leveraged ETF momentum, top-2, weekly

Core modes (set via PORTFOLIO_MODE env var):
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
INITIAL_CAPITAL = 100_000

# ── Two-Tranche Structure ────────────────────────────────────────
# Core tranche:       $90,000 — standard balanced strategy
# Aggressive tranche: $10,000 — leveraged ETF rotation, no stocks
AGGRESSIVE_TRANCHE_PCT = 0.10    # 10% = $10,000

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

# ── Aggressive Tranche Parameters ($10,000) ─────────────────────
# Pure leveraged ETF momentum — top-2 picks, weekly rotation.
# Tight stops because leveraged ETFs decay rapidly if held through drawdowns.
AGGRESSIVE_PARAMS = {
    "momentum_top_n": 2,            # hold only the top-2 leveraged ETFs
    "max_position_pct": 0.50,       # up to $5,000 per position
    "stop_loss_pct": 0.10,          # cut at -10% (tight — leveraged decay is costly)
    "trailing_stop_pct": 0.15,      # trail at -15% from peak
    "rebalance_days": 7,            # weekly rotation
    "cash_buffer_pct": 0.05,        # keep $500 in cash as reserve
}

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

# ── Alpaca broker ───────────────────────────────────────────────
ALPACA_ENV = os.environ.get("ALPACA_ENV", "paper")         # "paper" | "live"
ALPACA_LIVE_CONFIRM = os.environ.get("ALPACA_LIVE_CONFIRM") == "yes"
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")

# Alpaca API endpoints. The SDK picks these automatically from env, but
# surfaced here for clarity / dry-run / test overrides.
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL = "https://api.alpaca.markets"

# ── Safety rails ────────────────────────────────────────────────
HALT_PATH = os.path.join(os.path.dirname(__file__), ".cache", "HALT")
DAILY_TRADE_LOG = os.path.join(os.path.dirname(__file__), ".cache", "daily_trade_log.json")
PENDING_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "pending_orders.json")

DAILY_MAX_ORDERS = 40
DAILY_MAX_NOTIONAL = 25_000
LARGE_ORDER_THRESHOLD = 2_000
PENDING_ORDER_TTL_HOURS = 6

# ── Rebalance cadence per tranche ───────────────────────────────
# Core cadence comes from the active mode; aggressive is fixed (weekly).
REBALANCE_DAYS = {
    "core": _params["rebalance_days"],
    "aggressive": AGGRESSIVE_PARAMS["rebalance_days"],
}

# ── Intraday execution layer ────────────────────────────────────

EXECUTOR_WINDOW_START = "10:00"         # ET (avoids 9:30 open auction)
EXECUTOR_WINDOW_END   = "15:50"         # ET (leaves room for end-of-day cleanup)
EXECUTOR_TICK_MINUTES = 10
EXECUTOR_SHADOW_MODE  = True            # Phase 0: log intended submissions only
PLANNER_DIRECT_SUBMIT_THRESHOLD = 500.0  # USD: below this, planner submits immediately

EXECUTION_TIERS = {
    "HIGH": {"etf_bps": 50, "stock_bps": 100},
    "MED":  {"etf_bps": 30, "stock_bps": 50},
}
AGGRESSIVE_TIER_MULTIPLIER = 1.5
MACRO_EXIT_TOLERANCE_BPS   = 150        # overrides HIGH for macro-driven exits

# Slice count by (tier, notional bucket). "small" = $500–$2000, "large" = ≥$2000.
SLICE_COUNTS = {
    "HIGH": {"small": 2, "large": 2},
    "MED":  {"small": 2, "large": 4},
}
SLICE_SIZE_SMALL_MAX = 2000.0

CIRCUIT_BREAKERS = {
    "spy_drop_pct":           0.015,    # A: SPY drop from baseline
    "vix_multiplier":         1.5,      # B: VIX vs baseline
    "vix_absolute":           25.0,     # B: absolute VIX floor
    "single_name_drop_pct":   0.05,     # C: per-symbol drop from baseline
    "news_corroboration_pct": 0.005,    # D: SPY move to corroborate news
    "news_window_minutes":    15,       # D: corroboration lookback
    "news_dedupe_minutes":    60,       # D: title-hash dedupe window
    "macro_drop":             0.3,      # E: macro score drop
}

NEWS_SHOCK_KEYWORDS = [
    "tariff", "tariffs", "sanctions",
    "rate cut", "rate hike", "fed", "powell", "fomc",
    "war", "military", "invasion",
    "shutdown", "default", "recession",
]

# Breaker E exempts these from abort (rotating into them is the right response to macro stress).
DEFENSIVE_SYMBOLS = {"BIL", "SHY", "IEF", "TLT"}

# Pending plan persistence
PENDING_PLAN_PATH = os.path.join(os.path.dirname(__file__), ".cache", "pending_plan.json")
NEWS_SHOCK_LOG    = os.path.join(os.path.dirname(__file__), ".cache", "news_shock_log.csv")
TELEGRAM_NOTIFY_PATH = os.path.join(os.path.dirname(__file__), ".cache",
                                    "telegram_notifications.json")

# ── Strategy overrides (written by quant review subagent) ────────
import json
import logging as _logging

_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), ".cache", "strategy_overrides.json")

# Allowlist: key → (expected_type, lower_bound, upper_bound)
# Bounds of None mean unbounded (for lists).
# The applier enforces relative-pct bounds (±20%, ±50%); this layer enforces
# absolute bounds as a second line of defense.
_OVERRIDE_SCHEMA = {
    # Low-risk (auto-applied by the quant review applier)
    "WATCHLIST":            (list,  None, None),
    "NEWS_SHOCK_KEYWORDS":  (list,  None, None),
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
    "CASH_BUFFER_PCT":      (float, 0.02, 0.20),
    # High-risk (require TG approval; applied here only after the bot
    # writes them to strategy_overrides.json on /strategy-approve)
    "MOMENTUM_TOP_N":            (int,   1,    10),
    "ETF_ALLOCATION_PCT":        (float, 0.0,  1.0),
    "STOCK_ALLOCATION_PCT":      (float, 0.0,  1.0),
    "SCREEN_MIN_ROE":            (float, 0.0,  1.0),
    "SCREEN_MAX_PE":             (float, 5.0,  100.0),
    "SCREEN_MAX_DEBT_EQUITY":    (float, 0.0,  10.0),
    "MOMENTUM_LOOKBACK_MONTHS":  (list,  None, None),
    "SAFE_HAVEN":                (str,   None, None),
}

def _apply_overrides():
    """Load and apply strategy overrides. Silent on missing/corrupt files."""
    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH) as f:
            overrides = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _logging.warning(f"config: strategy_overrides.json unreadable ({e}); using defaults")
        return
    if not isinstance(overrides, dict):
        _logging.warning("config: strategy_overrides.json not an object; using defaults")
        return
    for key, value in overrides.items():
        if key not in _OVERRIDE_SCHEMA:
            _logging.warning(f"config: ignoring override for unknown/forbidden key {key!r}")
            continue
        expected_type, lo, hi = _OVERRIDE_SCHEMA[key]
        if not isinstance(value, expected_type):
            _logging.warning(f"config: override for {key!r} has wrong type {type(value).__name__}")
            continue
        if lo is not None and not (lo <= value <= hi):
            _logging.warning(f"config: override for {key!r}={value} out of bounds [{lo},{hi}]")
            continue
        globals()[key] = value

_apply_overrides()
