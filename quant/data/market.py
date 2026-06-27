"""
Data fetching and caching layer using yfinance.

Cache files live under .cache/:
  - prices_*.csv      adjusted Close per ticker (set), TTL = CACHE_TTL_HOURS
  - ohlcv_*.parquet   MultiIndex OHLCV (field, ticker), TTL = CACHE_TTL_HOURS
  - info_*.json       per-ticker yf.Ticker.info dump,   TTL = CACHE_TTL_HOURS
  - fund_*.json       per-ticker CANSLIM C+A inputs,    TTL = FUNDAMENTALS_TTL_HOURS

All writes go through fileio.atomic_write_* (fcntl lock + tmp+rename),
so concurrent cron jobs hitting the same key never clobber each other or
read a half-flushed file.
"""
import os
from quant import paths
import json
import time
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
import yfinance as yf
import pandas as pd
import numpy as np

from quant.infra.fileio import atomic_write_csv, atomic_write_parquet, atomic_write_json

_log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(paths.REPO_ROOT, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_TTL_HOURS = 4              # prices / ohlcv / info
FUNDAMENTALS_TTL_HOURS = 24      # quarterly EPS / revenue (changes slowly)

_DOWNLOAD_TIMEOUT = 60   # seconds for yf.download() batch calls
_TICKER_TIMEOUT   = 15   # seconds for per-ticker yfinance attribute calls
_RETRY_BACKOFF_MS = 300

# Module-level executor shared by all _run_with_timeout calls.
# 4 workers handles the typical concurrency (a few simultaneous fetches);
# previous code allocated a fresh single-worker pool per call which had
# noticeable fork/join overhead under intraday cron load.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="data-yf")


def _run_with_timeout(fn, *args, timeout: int = 30, **kwargs):
    """Run fn(*args, **kwargs) with a wall-clock timeout via the shared
    executor. Raises TimeoutError on expiry."""
    future = _EXECUTOR.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except _FuturesTimeout:
        raise TimeoutError(f"yfinance call timed out after {timeout}s")


def _retry(fn, attempts: int = 2):
    """Call fn() with up to `attempts` total tries. Brief sleep between.
    Re-raises the last exception if all attempts fail. yfinance / network
    blips are common enough that a single retry materially improves cron
    success rate."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(_RETRY_BACKOFF_MS / 1000.0)
    raise last_exc  # type: ignore[misc]


def _normalize_tickers(tickers) -> list:
    """Accept either a string or an iterable; always return a list.

    Without this, `fetch_prices("NVDA")` (forgot the brackets) generates a
    cache key from `sorted("NVDA")` = ["A","D","N","V"], produces garbage
    files like `prices_A_D_N_V_2y.csv`, and every distinct typo creates a
    new garbage file. Silent — the data returned was correct, only cache
    was broken. Normalize at the entry point.
    """
    if isinstance(tickers, str):
        return [tickers]
    return list(tickers)


def _cache_key(prefix: str, tickers: list, period: str) -> str:
    """Filesystem-safe key. Short ticker sets use raw join (debuggable),
    long ones fall back to md5. Same rule for prices and ohlcv now."""
    raw = f"{prefix}_{'_'.join(sorted(tickers))}_{period}"
    if len(raw) <= 200:
        return raw
    return f"{prefix}_" + hashlib.md5(raw.encode()).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.csv")


def _cache_path_parquet(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.parquet")


def _is_fresh(path: str, ttl_hours: float = CACHE_TTL_HOURS) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < ttl_hours * 3600


def fetch_prices(tickers, period: str = "2y") -> pd.DataFrame:
    """Fetch adjusted close prices for a list of tickers (or one string).

    Returns a DataFrame with DatetimeIndex × ticker columns. Empty
    DataFrame on empty input or persistent yfinance failure.
    """
    tickers = _normalize_tickers(tickers)
    if not tickers:
        return pd.DataFrame()
    path = _cache_path(_cache_key("prices", tickers, period))

    if _is_fresh(path):
        try:
            return pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception as e:
            _log.warning("fetch_prices: cache read failed (%s); refetching", e)

    def _do_fetch():
        return _run_with_timeout(
            yf.download, tickers, period=period,
            auto_adjust=True, progress=False, timeout=_DOWNLOAD_TIMEOUT,
        )

    try:
        raw = _retry(_do_fetch)
    except Exception as e:
        _log.warning("fetch_prices: yfinance failed for %r: %s", tickers, e)
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        df = raw["Close"]
    else:
        df = raw[["Close"]]
        df.columns = tickers
    df = df.dropna(how="all")
    try:
        atomic_write_csv(path, df)
    except Exception as e:
        _log.warning("fetch_prices: cache write failed for %s: %s", path, e)
    return df


def fetch_ohlcv(tickers, period: str = "1y") -> pd.DataFrame:
    """Fetch OHLCV for tickers. Always returns MultiIndex columns (field × ticker)
    regardless of how many tickers — callers can stop guessing the shape.
    """
    tickers = _normalize_tickers(tickers)
    if not tickers:
        return pd.DataFrame()
    path = _cache_path_parquet(_cache_key("ohlcv", tickers, period))

    if _is_fresh(path):
        try:
            return pd.read_parquet(path)
        except Exception as e:
            _log.warning("fetch_ohlcv: cache read failed (%s); refetching", e)

    def _do_fetch():
        return _run_with_timeout(
            yf.download, tickers, period=period,
            auto_adjust=True, progress=False, timeout=_DOWNLOAD_TIMEOUT,
        )

    try:
        df = _retry(_do_fetch)
    except Exception as e:
        _log.warning("fetch_ohlcv: yfinance failed for %r: %s", tickers, e)
        return pd.DataFrame()

    df = df.dropna(how="all")

    # Always normalize to MultiIndex (field, ticker). yfinance returns FLAT
    # columns for single-ticker downloads (and sometimes for multi-ticker
    # too on certain versions) — flat shape forced every downstream consumer
    # (sepa_exits, screener, watchdog) to implement its own shape-detection
    # fallback. Now they don't.
    if not isinstance(df.columns, pd.MultiIndex):
        # Flat columns like ['Open', 'High', 'Low', 'Close', 'Volume']
        df.columns = pd.MultiIndex.from_tuples(
            [(c, tickers[0]) for c in df.columns]
        )

    try:
        atomic_write_parquet(path, df)
    except Exception as e:
        _log.warning("fetch_ohlcv: cache write failed for %s: %s", path, e)
    return df


def fetch_info(ticker: str) -> dict:
    """Fetch raw yf.Ticker(ticker).info dict (cached as JSON).

    Fail-open: returns {} on any error. Matches fetch_fundamentals'
    contract — previously fetch_info raised on yfinance failures while
    fetch_fundamentals silently swallowed them, forcing every caller to
    know which one to wrap in try/except.
    """
    path = os.path.join(CACHE_DIR, f"info_{ticker}.json")
    if _is_fresh(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            _log.warning("fetch_info: cache read failed for %s: %s", ticker, e)

    def _do_fetch():
        return _run_with_timeout(
            lambda: yf.Ticker(ticker).info, timeout=_TICKER_TIMEOUT,
        )

    try:
        info = _retry(_do_fetch)
    except Exception as e:
        _log.warning("fetch_info: yfinance failed for %s: %s", ticker, e)
        return {}

    info = info or {}
    try:
        atomic_write_json(path, info)
    except Exception as e:
        _log.warning("fetch_info: cache write failed for %s: %s", ticker, e)
    return info


def fetch_fundamentals(ticker: str) -> dict:
    """Fetch quarterly/annual EPS and revenue metrics for CANSLIM C+A filters.

    Returns dict with (any may be absent if the upstream API didn't provide):
      eps_q_growth   float  — most recent quarter EPS YoY (e.g. 0.35 = 35%)
      revenue_growth float  — TTM revenue YoY
      quarterly_eps  list[float]   — last N quarters EPS, most recent first
      annual_eps     list[float]   — last N years EPS, most recent first

    TTL: FUNDAMENTALS_TTL_HOURS. Fail-open: returns {} on any error.
    Reuses fetch_info's cache for the .info portion so callers don't double-
    pay the yf.Ticker(...).info round-trip when both functions are called.

    Each yfinance failure is logged at WARNING level — screener fails open
    on empty fundamentals (lets every ticker through C+A), so silent yfinance
    breakage would silently disable the entire fundamental filter; logging
    lets ops see the degradation.
    """
    cache_path = os.path.join(CACHE_DIR, f"fund_{ticker}.json")
    if _is_fresh(cache_path, ttl_hours=FUNDAMENTALS_TTL_HOURS):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception as e:
            _log.warning("fetch_fundamentals: cache read failed for %s: %s", ticker, e)

    result: dict = {}

    # Reuse fetch_info's cache for the growth-rate fields.
    info = fetch_info(ticker)
    if info:
        for key, dest in (("earningsQuarterlyGrowth", "eps_q_growth"),
                          ("revenueGrowth", "revenue_growth")):
            v = info.get(key)
            if v is not None and isinstance(v, (int, float)) and v == v and abs(v) != float("inf"):
                result[dest] = float(v)
    else:
        _log.warning("fetch_fundamentals: %s: empty .info — growth fields will be missing", ticker)

    t = yf.Ticker(ticker)

    def _q_stmt():
        return _run_with_timeout(lambda: t.quarterly_income_stmt, timeout=_TICKER_TIMEOUT)

    try:
        q_stmt = _retry(_q_stmt)
        if q_stmt is not None and not q_stmt.empty:
            for label in ("Basic EPS", "Diluted EPS"):
                if label in q_stmt.index:
                    series = q_stmt.loc[label].dropna().sort_index(ascending=False)
                    result["quarterly_eps"] = [float(v) for v in series.values]
                    break
    except Exception as e:
        _log.warning("fetch_fundamentals: %s: quarterly_income_stmt failed: %s", ticker, e)

    def _a_stmt():
        return _run_with_timeout(lambda: t.income_stmt, timeout=_TICKER_TIMEOUT)

    try:
        a_stmt = _retry(_a_stmt)
        if a_stmt is not None and not a_stmt.empty:
            for label in ("Basic EPS", "Diluted EPS"):
                if label in a_stmt.index:
                    series = a_stmt.loc[label].dropna().sort_index(ascending=False)
                    result["annual_eps"] = [float(v) for v in series.values]
                    break
    except Exception as e:
        _log.warning("fetch_fundamentals: %s: annual income_stmt failed: %s", ticker, e)

    try:
        atomic_write_json(cache_path, result)
    except Exception as e:
        _log.warning("fetch_fundamentals: cache write failed for %s: %s", ticker, e)

    return result


_ESTIMATES_EMPTY = {"revision_trend": None, "up_revisions_90d": None,
                    "down_revisions_90d": None, "surprises": []}


def fetch_estimates(ticker: str) -> dict:
    """Analyst EPS-estimate revision trend + recent earnings-surprise history.
    Fail-open: returns the all-None/empty shape on any error or missing data."""
    def _do():
        t = yf.Ticker(ticker)
        up = dn = None
        try:
            rev = t.eps_revisions            # DataFrame indexed by period
            if rev is not None and not rev.empty:
                up = int(rev.get("upLast30days", rev.iloc[:, 0]).fillna(0).sum())
                dn = int(rev.get("downLast30days", rev.iloc[:, -1]).fillna(0).sum())
        except Exception:
            up = dn = None
        trend = None
        if up is not None and dn is not None:
            trend = "rising" if up > dn else "falling" if dn > up else "flat"
        surprises = []
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and "Surprise(%)" in ed.columns:
                surprises = [float(x) / 100.0 for x in ed["Surprise(%)"].dropna().head(4)]
        except Exception:
            surprises = []
        return {"revision_trend": trend, "up_revisions_90d": up,
                "down_revisions_90d": dn, "surprises": surprises}
    try:
        return _run_with_timeout(_do, timeout=_TICKER_TIMEOUT)
    except Exception as e:
        _log.warning("fetch_estimates: %s failed: %s", ticker, e)
        return dict(_ESTIMATES_EMPTY)


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Daily simple returns."""
    return prices.pct_change().dropna()


def compute_log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return np.log(prices / prices.shift(1)).dropna()
