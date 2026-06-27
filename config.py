"""
Portfolio configuration for a quantitative investment system.

Capital structure (two-tranche, sized dynamically from Alpaca equity):
  Core tranche       (1 - AGGRESSIVE_TRANCHE_PCT) of system equity
                     — ETF rotation + CANSLIM stock screen
  Aggressive tranche AGGRESSIVE_TRANCHE_PCT of system equity
                     — leveraged ETF momentum, top-2

Both tranches rebalance DAILY; the REBALANCE_BAND_PCT (5% of tranche capital)
is the actual churn brake — drifts smaller than the band are treated as holds.

Core modes (set via PORTFOLIO_MODE env var):
  conservative — capital preservation, ETF-heavy, wide stops
  balanced     — default, mix of ETFs and stocks
  growth       — aggressive, small/mid-cap heavy, leveraged ETFs in core too
"""
import os
import json  # used by _load_auto_watchlist() at module load — must precede it

# ── Portfolio Mode ──────────────────────────────────────────────
# Set via: PORTFOLIO_MODE=growth python3 run.py
# Or change the default here
PORTFOLIO_MODE = os.environ.get("PORTFOLIO_MODE", "balanced")

# ── Capital ─────────────────────────────────────────────────────
INITIAL_CAPITAL = 100_000

# ── Two-Tranche Structure ────────────────────────────────────────
# Tranche capital is computed at runtime from snap.equity (see
# rebalancer._system_equity), so the system compounds as the account grows.
# INITIAL_CAPITAL above is just a defensive fallback if the live equity
# fetch fails on a brand-new account.
AGGRESSIVE_TRANCHE_PCT = 0.10    # 10% to aggressive sleeve

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
        "rebalance_days": 1,             # daily — 5% REBALANCE_BAND_PCT throttles churn
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
        "rebalance_days": 1,             # daily — 5% REBALANCE_BAND_PCT throttles churn
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
        "rebalance_days": 1,             # daily — 5% REBALANCE_BAND_PCT throttles churn
        "use_leveraged_etfs": True,      # allow TQQQ, SOXL, etc.
    },
}

if PORTFOLIO_MODE not in _MODE_PARAMS:
    raise ValueError(
        f"unknown PORTFOLIO_MODE: {PORTFOLIO_MODE!r}; "
        f"expected one of {sorted(_MODE_PARAMS)}"
    )

# Internal: callers should NEVER read `config._params["x"]` directly.
# Always use the module-level constants (STOP_LOSS_PCT, etc.) derived below
# — they're what the quant subagent's _OVERRIDE_SCHEMA targets, and what
# tests monkeypatch. Reading the dict bypasses overrides.
_params = _MODE_PARAMS[PORTFOLIO_MODE]

MAX_POSITION_PCT = _params["max_position_pct"]
CASH_BUFFER_PCT = _params["cash_buffer_pct"]
STOP_LOSS_PCT = _params["stop_loss_pct"]
TRAILING_STOP_PCT = _params["trailing_stop_pct"]
ETF_ALLOCATION_PCT = _params["etf_allocation_pct"]
STOCK_ALLOCATION_PCT = _params["stock_allocation_pct"]
USE_LEVERAGED_ETFS = _params["use_leveraged_etfs"]

# ── Position-adoption & stop-enforcement flags ──────────────────
# Broker-imported positions (manual trades, legacy holdings) arrive with no
# local metadata. When True, sync_state tags them into a sleeve so the
# rebalancer manages them and stop logic applies. When False, they stay
# 'unknown' (legacy behavior).
ADOPT_EXTERNAL_POSITIONS = True

# Defense-in-depth: if untagged ('unknown') market value exceeds this fraction
# of equity, sync_state raises a loud alert — the rebalancer would otherwise
# silently size itself to near-zero capital.
UNKNOWN_MV_HALT_PCT = 0.20

# When True, the intraday watchdog submits a market sell on a stop/trailing
# breach (works on fractional shares, unlike native stop orders). When False,
# the watchdog only alerts (legacy behavior).
ENFORCE_STOPS = True

# ── Aggressive Tranche Parameters ───────────────────────────────
# Pure leveraged ETF momentum — top-2 picks, daily rebalance cadence
# (5% REBALANCE_BAND_PCT throttles churn; hysteresis prevents whipsaw).
# Tight stops because leveraged ETFs decay rapidly if held through drawdowns.
AGGRESSIVE_PARAMS = {
    "momentum_top_n": 2,            # hold only the top-2 leveraged ETFs
    "stop_loss_pct": 0.10,          # cut at -10% (tight — leveraged decay is costly)
    "trailing_stop_pct": 0.15,      # trail at -15% from peak
    "rebalance_days": 1,            # daily — 5% REBALANCE_BAND_PCT throttles churn
    "cash_buffer_pct": 0.05,        # keep $500 in cash as reserve
    "hysteresis_depth": 1,          # held leveraged ETF kept until rank > top_n + depth
}

# ── Stop-loss ATR scaling (core tranche only) ───────────────────
# Initial stop = clamp(ATR_STOP_MULTIPLIER × ATR(ATR_PERIOD) / last_close,
#                      ATR_STOP_FLOOR_PCT, STOP_LOSS_PCT).
# Aggressive tranche keeps the fixed AGGRESSIVE_PARAMS["stop_loss_pct"].
# The floor stops near-zero-vol instruments (BIL, T-bill ETFs) from getting
# an absurdly tight ATR stop that would fire on bid-ask noise. Defensive /
# safe-haven symbols skip ATR scaling entirely (see orders._effective_stop_pct).
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
ATR_STOP_FLOOR_PCT = 0.02

# ── SEPA take-profit (Phase 1: core tranche only) ────────────────
# R-multiple scale-out: at each tier, sell `fraction` of initial_qty.
# After the final tier fills, the trailing-stop is cancelled and the
# remaining position is exited when daily close < EMA(SEPA_MA_PERIOD).
SEPA_ENABLED = True
SEPA_R_TIERS = [(2.0, 1/3), (3.0, 1/3)]   # (R-multiple, fraction-of-initial-qty)
# Enforce monotonic-ascending R so sepa_exits.next_r_tier_action can bail on
# the first unfilled-but-not-reached tier without missing a lower one.
_r_multiples = [r for r, _ in SEPA_R_TIERS]
if _r_multiples != sorted(_r_multiples):
    raise ValueError(
        f"SEPA_R_TIERS must be ascending in R-multiple, got {_r_multiples}"
    )
SEPA_MA_PERIOD = 21
SEPA_MA_TYPE = "ema"                       # "ema" | "sma"
SEPA_MA_HISTORY = "6mo"                    # data.fetch_prices period for the EMA

# ── SEPA Phase 2 — failed-breakout ──────────────────────────────
SEPA_FAILED_BREAKOUT_WINDOW_DAYS = 3
ENTRY_PIVOTS_PATH = os.path.join(os.path.dirname(__file__),
                                  ".cache", "entry_pivots.json")

# ── SEPA Phase 2 — climax detection ─────────────────────────────
SEPA_CLIMAX_RETURN_LOOKBACK = 8
SEPA_CLIMAX_RETURN_THRESHOLD = 0.25
SEPA_CLIMAX_RANGE_LOOKBACK = 20
SEPA_CLIMAX_RANGE_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_LOOKBACK = 20
SEPA_CLIMAX_VOLUME_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_RECENT_DAYS = 3
SEPA_CLIMAX_TRAIL_PCT = 0.06       # 6% — half of default core trail

# ── Cash / margin policy ────────────────────────────────────────
# When False, execute_plan rejects any group of buys whose total
# notional exceeds available cash (i.e. would push the account into
# margin). Set True only when you explicitly want the system to
# take leverage. Sells are always allowed.
ALLOW_MARGIN = False

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

ETF_LEVERAGED = [
    # Leveraged (used by aggressive tranche unconditionally; added to core
    # universe only in growth mode). 2x/3x daily — high decay if held in
    # drawdowns.
    "TQQQ",  # 3x Nasdaq-100
    "SOXL",  # 3x Semiconductors
    "UPRO",  # 3x S&P 500
    "TNA",   # 3x Small-Cap
    "TECL",  # 3x Technology
    "LABU",  # 3x Biotech
]

# Backward-compat alias for the leading-underscore name. Old callers
# (rebalancer / watchdog / tests) read config._ETF_LEVERAGED — preserved
# so this rename doesn't cascade into a sweep of unrelated changes.
_ETF_LEVERAGED = ETF_LEVERAGED

ETF_UNIVERSE = _ETF_BASE + (ETF_LEVERAGED if USE_LEVERAGED_ETFS else [])

SAFE_HAVEN = "BIL"               # T-bill ETF (cash equivalent)
MOMENTUM_LOOKBACK_MONTHS = [1, 3, 6, 12]
MOMENTUM_TOP_N = _params["momentum_top_n"]
SMA_FILTER_PERIOD = 200          # 200-day SMA trend filter

# Hysteresis depth for ETF momentum selection. A held ETF that slips out of
# top-MOMENTUM_TOP_N is kept as long as it stays within
# top-(MOMENTUM_TOP_N + MOMENTUM_HYSTERESIS_DEPTH) AND remains above its
# 200-day SMA. Prevents whipsaw when an ETF oscillates around the cutoff
# rank. Set to 0 to disable.
MOMENTUM_HYSTERESIS_DEPTH = 1

# ── Strategy 2: CANSLIM Technical Stock Screen ──────────────────
# Satellite allocation (20% of portfolio): momentum + base pattern filter.

SCREEN_TOP_N = 10                # screener returns top 10
STOCK_SLEEVE_TOP_N = 3           # rebalancer picks this many from screener output

# Relative Strength
SCREEN_RS_MIN = 75               # RS percentile vs universe (0-100); 75+ = leadership

# Average Daily Range — volatility/tradability filter
SCREEN_ADR_MIN = 0.04            # 4% minimum average daily range
SCREEN_ADR_PERIOD = 20           # trading days for ADR calculation

# EMA trend filter — price must be above both to pass
SCREEN_EMA_FAST = 21
SCREEN_EMA_SLOW = 50

# Base pattern (medium sophistication: tight box + volume contraction)
SCREEN_BASE_WEEKS_MIN = 5        # minimum consolidation weeks
SCREEN_BASE_WEEKS_MAX = 15       # maximum consolidation weeks
SCREEN_BASE_DEPTH_MAX = 0.30     # max drawdown within base (30%)

# CANSLIM C+A fundamental filters (applied before technical screen)
SCREEN_EPS_Q_GROWTH_MIN = 0.25   # quarterly EPS YoY growth >= 25% (C)
SCREEN_REV_GROWTH_MIN = 0.20     # TTM/quarterly revenue YoY growth >= 20% (C)

# ── Stock Discovery ─────────────────────────────────────────────
# discovery.py composite score weights. Each dimension is ranked
# cross-sectionally (pct percentile, 0-100) then weighted. Weights
# don't need to sum to 1 — they're applied as multipliers.
DISCOVERY_WEIGHTS = {
    "rs":            0.25,   # relative strength percentile (3M/6M/12M blend)
    "rev_growth":    0.15,   # revenue YoY
    "eps_q_growth":  0.15,   # latest quarterly EPS YoY
    "roe":           0.10,   # return on equity
    "mom_3m":        0.10,   # 3-month price return
    "dist_52w_high": 0.10,   # distance to 52-week high (closer = higher)
    "ipo_age":       0.05,   # younger US IPOs score higher (CANSLIM "N")
    "sma50_dist":    0.05,   # % above 50-day SMA (continuous)
    "value_pe":      0.05,   # inverse P/E percentile (loss-makers excluded)
}
DISCOVERY_THREAD_WORKERS = 8         # yfinance concurrent fetchers
DISCOVERY_STALE_DAYS = 90            # --prune: stale if not seen in N days
DISCOVERY_REQUIRE_US = True          # screen out non-US-domiciled tickers
DISCOVERY_TICKER_SOURCES = (         # smart-money signals to harvest
    "13F", "etf-holdings", "ark", "congress",
)
# ── Discovery scan universe (方案A: multi-index via Wikipedia) ──────────────
# The discovery scan universe is the union of these index constituent lists,
# scraped from Wikipedia (geo-neutral). S&P 500 + Nasdaq-100 + S&P 400 MidCap is
# ~850 large+mid-cap US names — far broader than the S&P 500 alone and includes
# growth leaders outside it (e.g. MRVL, via the Nasdaq-100). Geo-neutral by design:
# the iShares US holdings CSV is gated behind a country disclaimer for non-US
# accounts. discovery.get_universe_tickers falls back to S&P 500 alone if needed.
DISCOVERY_UNIVERSE_INDICES = ("sp500", "nasdaq100", "sp400")
DISCOVERY_UNIVERSE_MAX = 2000          # hard safety ceiling on universe size
# Two-stage screening: Stage 1 (cheap, batched OHLCV) ranks the whole universe on
# relative strength + liquidity and carries this many survivors into Stage 2
# (expensive per-ticker info+fundamentals).
DISCOVERY_STAGE1_KEEP = 250
DISCOVERY_MIN_PRICE = 5.0              # Stage-1 gate: drop sub-$5 names
DISCOVERY_MIN_DOLLAR_VOLUME = 5e6      # Stage-1 gate: avg daily $-volume floor
# Peer-relative ranking: rank value/quality factors within GICS sector so a
# high-P/E growth leader isn't graded against utilities/staples.
DISCOVERY_SECTOR_RELATIVE = True
# Growth exemption (rank-based, scale-invariant): names whose rev-growth percentile
# is >= this are NOT penalized on the value-P/E factor (neutralized to 50).
DISCOVERY_GROWTH_EXEMPT_PCTL = 66.0
DIVIDEND_WITHHOLDING_RATE = 0.30     # W-8BEN: 30% US withholding on dividends

# ── Intraday buy signals (watchdog) ─────────────────────────────
WATCHDOG_BUY_LOOKBACK_DAYS = 10        # down-day volume lookback window
WATCHDOG_BUY_SCREENER_CACHE_HOURS = 1  # screener result cache lifetime (hours)
WATCHDOG_BUY_MIN_ELAPSED_MIN = 30      # skip volume estimate if < N min into trading day
WATCHDOG_BUY_NOTIONAL = 2000.0         # USD notional per buy signal

# ── Rebalancing cost model ──────────────────────────────────────
# Used by backtest.py only — production cadence comes from REBALANCE_DAYS below.
TRANSACTION_COST_BPS = 5         # ~$0.05 per $100 traded

# ── Candidate individual stocks for deeper analysis ─────────────
# Hand-curated SEED watchlist. This literal is owned by humans — keep the
# comments and grouping; discovery.py NEVER rewrites this block. Auto-
# discovered names live in watchlist_auto.json (see below) and are unioned
# in to form the final WATCHLIST.
WATCHLIST_SEED = [
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

# Auto-discovered tickers (appended by `discovery.py --update`, trimmed by
# `--prune --confirm`). A generated file — never hand-edit it; edit
# WATCHLIST_SEED above instead.
WATCHLIST_AUTO_PATH = os.path.join(os.path.dirname(__file__), "watchlist_auto.json")


def _is_valid_ticker(t) -> bool:
    """Same validity rule discovery uses: non-empty alpha (dots/dashes ok), len<=5."""
    return (
        isinstance(t, str)
        and bool(t)
        and t.replace(".", "").replace("-", "").isalpha()
        and len(t) <= 5
    )


def _load_auto_watchlist() -> list:
    """Load auto-discovered tickers from WATCHLIST_AUTO_PATH.

    Fail-open: a missing or corrupt file (or any non-list/garbage payload)
    yields [] so the seed list is used alone — discovery problems must never
    crash config import.
    """
    path = WATCHLIST_AUTO_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _logging.warning(f"config: watchlist_auto.json unreadable ({e}); using seed only")
        return []
    if not isinstance(data, list):
        _logging.warning("config: watchlist_auto.json not a list; using seed only")
        return []
    out, seen = [], set()
    for t in data:
        if _is_valid_ticker(t) and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _union_watchlist(seed: list, auto: list) -> list:
    """Seed first, then auto-only names; deduped, order preserved."""
    out = list(seed)
    seen = set(seed)
    for t in auto:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


WATCHLIST_AUTO = _load_auto_watchlist()
# Final list every consumer (screener / sentiment / discovery.merge_candidates)
# reads. The quant-subagent override allowlist still targets this name.
WATCHLIST = _union_watchlist(WATCHLIST_SEED, WATCHLIST_AUTO)

# ── Value+Quality screen + ensemble ─────────────────────────────
VS_MIN_DOLLAR_VOLUME = 2_000_000     # ADV * price liquidity gate
VS_MIN_PRICE = 5.0                   # no penny stocks
VS_MIN_MARKET_CAP = 300_000_000      # no micro-caps
VS_TOP_N = 20                        # value_screen emits this many
VS_WEIGHTS = {"value": 0.5, "quality": 0.35, "improving": 0.15}
ENSEMBLE_TOP_N = 4                   # agent's final buy candidates
ENSEMBLE_STRATEGIES = ["value", "canslim"]   # registered strategy names
ENSEMBLE_CANDIDATES_MAX_AGE_HOURS = 24       # buy_candidates.json staleness limit

# ── Alpaca broker ───────────────────────────────────────────────
# ALPACA_LIVE_CONFIRM is intentionally not surfaced here — broker.py reads it
# directly from os.environ to keep the safety check next to the construction
# logic. Duplicating it as a module constant invited "config.ALPACA_LIVE_CONFIRM"
# misuse (the constant was a snapshot at import time; the env can change).
ALPACA_ENV = os.environ.get("ALPACA_ENV", "paper")         # "paper" | "live"
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET")

# ── Safety rails ────────────────────────────────────────────────
HALT_PATH = os.path.join(os.path.dirname(__file__), ".cache", "HALT")
DAILY_TRADE_LOG = os.path.join(os.path.dirname(__file__), ".cache", "daily_trade_log.json")
PENDING_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "pending_orders.json")

DAILY_MAX_ORDERS = 40
# Paper mode: no notional cap so full portfolio deploys in one session.
DAILY_MAX_NOTIONAL = 200_000 if ALPACA_ENV == "paper" else 25_000
# Orders >= this notional require Telegram approval; orders < this auto-submit.
LARGE_ORDER_THRESHOLD = 50_000
PENDING_ORDER_TTL_HOURS = 6

# ── Rebalance cadence per tranche ───────────────────────────────
# Core cadence comes from the active mode; aggressive is fixed (weekly).
REBALANCE_DAYS = {
    "core": _params["rebalance_days"],
    "aggressive": AGGRESSIVE_PARAMS["rebalance_days"],
}

# Drift threshold below which reconcile_to_targets treats a position as "hold"
# (fraction of tranche capital). With daily rebalance cadence, this band is the
# primary churn-suppression mechanism, so keep it generous.
REBALANCE_BAND_PCT = 0.05

# ── Intraday execution layer ────────────────────────────────────

EXECUTOR_WINDOW_START = "10:00"         # ET (avoids 9:30 open auction)
EXECUTOR_WINDOW_END   = "15:50"         # ET (leaves room for end-of-day cleanup)
EXECUTOR_TICK_MINUTES = 10
EXECUTOR_SHADOW_MODE  = False           # live order submission enabled
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
import logging as _logging

_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), ".cache", "strategy_overrides.json")

# Allowlist: key → (expected_type, lower_bound, upper_bound).
# For numeric types: lo/hi are value bounds.
# For list types:    lo/hi are MIN/MAX length (so quant subagent can't push
#                    e.g. an empty WATCHLIST or a 10K-ticker garbage list).
# For str types:     lo/hi are unused — None/None.
# The applier enforces relative-pct bounds (±20%, ±50%); this layer enforces
# absolute bounds as a second line of defense.
_OVERRIDE_SCHEMA = {
    # Low-risk (auto-applied by the quant review applier)
    "WATCHLIST":            (list,  1,    200),
    "NEWS_SHOCK_KEYWORDS":  (list,  1,    100),
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "ATR_STOP_MULTIPLIER":  (float, 1.0,  4.0),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
    "CASH_BUFFER_PCT":      (float, 0.02, 0.20),
    # High-risk (require TG approval; applied here only after the bot
    # writes them to strategy_overrides.json on /strategy-approve)
    "MOMENTUM_TOP_N":            (int,   1,    10),
    "ETF_ALLOCATION_PCT":        (float, 0.0,  1.0),
    "STOCK_ALLOCATION_PCT":      (float, 0.0,  1.0),
    "SCREEN_RS_MIN":             (float, 0.0,  100.0),
    "SCREEN_ADR_MIN":            (float, 0.01, 0.15),
    "SCREEN_EMA_FAST":           (int,   5,    50),
    "SCREEN_EMA_SLOW":           (int,   20,   200),
    "SCREEN_BASE_WEEKS_MIN":     (int,   3,    15),
    "SCREEN_BASE_WEEKS_MAX":     (int,   8,    52),
    "SCREEN_BASE_DEPTH_MAX":     (float, 0.10, 0.50),
    "MOMENTUM_LOOKBACK_MONTHS":  (list,  1,    12),
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
            _logging.warning(
                f"config: override for {key!r} has wrong type {type(value).__name__}"
            )
            continue
        # For lists: lo/hi mean MIN/MAX length, not value bounds.
        if isinstance(value, list):
            if lo is not None and not (lo <= len(value) <= hi):
                _logging.warning(
                    f"config: override for {key!r} list length {len(value)} "
                    f"out of bounds [{lo},{hi}]"
                )
                continue
        elif lo is not None and not (lo <= value <= hi):
            _logging.warning(
                f"config: override for {key!r}={value} out of bounds [{lo},{hi}]"
            )
            continue
        globals()[key] = value

_apply_overrides()
