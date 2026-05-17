"""
Data fetching and caching layer using yfinance.
"""
import os, json, time, hashlib, datetime as dt
import yfinance as yf
import pandas as pd
import numpy as np

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_TTL_HOURS = 4  # refresh after 4 hours


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.csv")


def _cache_path_parquet(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.parquet")


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < CACHE_TTL_HOURS * 3600


def fetch_prices(tickers, period: str = "2y") -> pd.DataFrame:
    """Fetch adjusted close prices for a list of tickers. Returns DataFrame with DatetimeIndex."""
    key = f"prices_{'_'.join(sorted(tickers))}_{period}"
    if len(key) > 200:
        key = "prices_" + hashlib.md5(key.encode()).hexdigest()
    path = _cache_path(key)

    if _is_fresh(path):
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df

    raw = yf.download(tickers, period=period, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        df = raw["Close"]
    else:
        df = raw[["Close"]]
        df.columns = tickers
    df = df.dropna(how="all")
    df.to_csv(path)
    return df


def fetch_ohlcv(tickers: list, period: str = "1y") -> pd.DataFrame:
    """Fetch OHLCV data for tickers. Returns MultiIndex DataFrame (field × ticker)."""
    key = "ohlcv_" + hashlib.md5(("_".join(sorted(tickers)) + period).encode()).hexdigest()
    path = _cache_path_parquet(key)

    if _is_fresh(path):
        return pd.read_parquet(path)

    df = yf.download(tickers, period=period, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex) and len(tickers) == 1:
        df.columns = pd.MultiIndex.from_tuples([(c[0], tickers[0]) for c in df.columns])
    df = df.dropna(how="all")
    df.to_parquet(path)
    return df


def fetch_info(ticker: str) -> dict:
    """Fetch fundamental info for a single ticker (cached as JSON)."""
    path = os.path.join(CACHE_DIR, f"info_{ticker}.json")
    if _is_fresh(path):
        with open(path) as f:
            return json.load(f)
    info = yf.Ticker(ticker).info
    with open(path, "w") as f:
        json.dump(info, f)
    return info


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Daily simple returns."""
    return prices.pct_change().dropna()


def compute_log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    return np.log(prices / prices.shift(1)).dropna()
