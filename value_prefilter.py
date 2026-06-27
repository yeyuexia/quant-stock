"""Stage-0 bulk pre-filter for the value screen: drop cheap / illiquid names
using only price + volume (no per-ticker .info), cutting the Russell 3000 to a
few hundred survivors before the expensive fundamentals fetch."""
import logging
import config

_log = logging.getLogger(__name__)


def _default_price_fn(tickers):
    """{ticker: (last_price, avg_dollar_volume)} via one batched OHLCV pull."""
    from data import fetch_ohlcv
    out = {}
    try:
        df = fetch_ohlcv(tickers, period="3mo")
    except Exception as e:
        _log.warning("value_prefilter: batch OHLCV failed: %s", e)
        return out
    if df is None or getattr(df, "empty", True):
        return out
    try:
        close, vol = df["Close"], df["Volume"]
    except Exception:
        return out
    cols = list(close.columns) if hasattr(close, "columns") else []
    for t in cols:
        try:
            c = close[t].dropna(); v = vol[t].dropna()
            if len(c) < 5:
                continue
            out[t] = (float(c.iloc[-1]), float((c * v).tail(20).mean()))
        except Exception:
            continue
    return out


def prefilter(tickers, *, price_fn=None, max_keep=None):
    price_fn = price_fn or _default_price_fn
    max_keep = max_keep if max_keep is not None else config.VS_PREFILTER_MAX
    data = price_fn(list(tickers)) or {}
    survivors = []
    for t, pv in data.items():
        try:
            price, dvol = float(pv[0]), float(pv[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price < config.VS_MIN_PRICE or dvol < config.VS_MIN_DOLLAR_VOLUME:
            continue
        survivors.append((t, dvol))
    survivors.sort(key=lambda x: -x[1])
    return [t for t, _ in survivors[:max_keep]]
