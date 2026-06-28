"""
Macro Regime Detection using FRED (Federal Reserve Economic Data)

Uses leading economic indicators to determine if the macro environment
favors risk assets or defensive positioning. This overlays on top of
the momentum strategy to improve timing.

Key indicators:
  1. Yield Curve (10Y-2Y spread) — inverted = recession warning
  2. Credit Spreads (HY OAS) — widening = stress
  3. Unemployment Claims — rising = slowdown
  4. ISM Manufacturing PMI — below 50 = contraction
  5. Fed Funds Rate trajectory — tightening vs easing
  6. CPI YoY — inflation regime
  7. Financial Conditions — Chicago Fed NFCI

Each indicator produces a score from -1 (bearish) to +1 (bullish).
The composite score drives portfolio risk adjustment.
"""
import os
from quant import paths
import re
import time
import datetime as dt
import pandas as pd

from quant.infra.fileio import atomic_write_csv

CACHE_DIR = os.path.join(paths.REPO_ROOT, ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Load .env via python-dotenv (was a brittle hand-rolled parser before —
# couldn't handle quoted values, escapes, etc., and inconsistent with
# rebalancer.py / executor.py which already used load_dotenv).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(paths.REPO_ROOT, ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on env vars already being set

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

_FRED_CACHE_TTL_SECONDS = 12 * 3600
_FRED_RETRY_BACKOFF_MS  = 300


def _sanitize_series_id(series_id: str) -> str:
    """Filename-safe form of a FRED series ID. Future-proof against IDs
    that might contain unexpected characters."""
    return re.sub(r"[^A-Z0-9_-]", "_", series_id)


def _fetch_fred_series(series_id: str, *, force: bool = False) -> pd.Series:
    """Fetch a single FRED series (or yfinance fallback for a few aliases).

    `force=True` ignores the 12h cache — used by `python3 macro.py --refresh`
    when a fresh Fed announcement / data print should override stale cache.
    """
    cache_path = os.path.join(CACHE_DIR, f"fred_{_sanitize_series_id(series_id)}.csv")
    if not force and os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < _FRED_CACHE_TTL_SECONDS:
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            return df.iloc[:, 0]

    if FRED_API_KEY:
        from fredapi import Fred
        fred = Fred(api_key=FRED_API_KEY)
        # 1-retry on transient FRED errors. Without this, a single network
        # blip makes the indicator return empty → composite gets diluted
        # toward 0 (see macro_regime_score normalization).
        last_exc = None
        for attempt in range(2):
            try:
                data = fred.get_series(series_id).dropna()
                atomic_write_csv(cache_path, data.to_frame(series_id))
                return data
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(_FRED_RETRY_BACKOFF_MS / 1000.0)
        # Both attempts failed — caller will see an empty series and (post-M8)
        # exclude this indicator from the composite, rather than poison it.
        return pd.Series(dtype=float)
    else:
        # No FRED key — use yfinance as fallback for the handful of mapped series
        return _fallback_fetch(series_id, cache_path)


def _fallback_fetch(series_id, cache_path):
    """Fallback: estimate key macro indicators from market data via yfinance."""
    import yfinance as yf

    # Map FRED series to yfinance tickers where possible
    yf_map = {
        "DGS10": "^TNX",    # 10Y yield
        "DGS2":  "^IRX",    # 13-week T-bill (proxy for short rate)
        "SP500": "^GSPC",   # S&P 500
    }

    if series_id in yf_map:
        ticker = yf_map[series_id]
        data = yf.download(ticker, period="5y", progress=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex):
                s = data["Close"].iloc[:, 0]
            else:
                s = data["Close"]
            s = s.dropna()
            atomic_write_csv(cache_path, s.to_frame(series_id))
            return s

    return pd.Series(dtype=float)


def get_yield_curve_signal():
    """Yield curve: 10Y-2Y spread.
    Inverted (<0) = strong recession signal = bearish.
    Steep (>1.5) = early expansion = bullish.

    Returns signal=0 + value=None when either leg is missing — composite
    score then excludes this indicator entirely (don't fabricate a 2Y
    proxy; that was the old bug — a hardcoded 4.0 would silently mislead
    when actual 2Y diverged from the 2021-era assumption).
    """
    try:
        y10 = _fetch_fred_series("DGS10")
        y2 = _fetch_fred_series("DGS2")
        if y10.empty or y2.empty:
            missing = "10Y" if y10.empty else "2Y"
            return {
                "signal": 0,
                "value": None,
                "label": f"Yield curve: {missing} unavailable",
            }
        spread = y10.iloc[-1] - y2.iloc[-1]

        if spread < -0.5:
            signal = -1.0
        elif spread < 0:
            signal = -0.5
        elif spread < 0.5:
            signal = 0.0
        elif spread < 1.5:
            signal = 0.5
        else:
            signal = 1.0

        return {
            "signal": signal,
            "value": spread,
            "label": f"10Y-2Y Spread: {spread:.2f}%",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"Yield curve: error ({e})"}


def get_credit_spread_signal():
    """Credit spread: HY OAS (ICE BofA, BAMLH0A0HYM2, in bps).
    Widening = stress = bearish. Tightening = calm = bullish."""
    try:
        oas = _fetch_fred_series("BAMLH0A0HYM2")
        if oas.empty:
            return {"signal": 0, "value": None, "label": "HY OAS: N/A"}

        spread = oas.iloc[-1]
        # Bands: stress >600, crisis >800. Neutral midpoint is 450
        # (close to the long-run median of ~400 bps; we leave a small
        # buffer so a slightly above-median print doesn't tip negative).
        if spread > 800:
            signal = -1.0
        elif spread > 600:
            signal = -0.5
        elif spread > 450:
            signal = 0.0
        elif spread > 350:
            signal = 0.5
        else:
            signal = 1.0

        return {
            "signal": signal,
            "value": spread,
            "label": f"HY OAS: {spread:.0f} bps",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"HY OAS: error ({e})"}


def get_unemployment_signal():
    """Unemployment rate + Sahm Rule recession indicator.

    Official Sahm Rule (Claudia Sahm 2019): the current 3-month moving
    average of U-3 unemployment rises ≥ 0.5pp above its lowest 3-month MA
    over the prior 12 months. The previous implementation here used a 6m
    MA and single-month minimum, which neither matched the rule nor had
    stable timing — fired earlier than official Sahm in some scenarios,
    later in others. Naming it 'Sahm' in the watchdog notification was
    misleading.
    """
    try:
        unemp = _fetch_fred_series("UNRATE")
        if unemp.empty or len(unemp) < 14:
            # Need 12 rolling-3m MAs → at least 14 monthly prints.
            return {"signal": 0, "value": None, "label": "Unemployment: N/A"}

        current = unemp.iloc[-1]
        ma3 = unemp.rolling(3).mean().dropna()
        if len(ma3) < 12:
            return {"signal": 0, "value": None, "label": "Unemployment: insufficient history"}

        # Sahm = current 3m MA − minimum of the last 12 3m MAs.
        # `.tail(12)` includes the current month's 3m MA in the comparison
        # window, matching the official definition.
        current_ma3 = ma3.iloc[-1]
        low_ma3_12 = ma3.tail(12).min()
        sahm = current_ma3 - low_ma3_12

        avg_12m = unemp.iloc[-12:].mean()
        if sahm >= 0.5:
            signal = -1.0  # Sahm Rule triggered
        elif current > avg_12m + 0.3:
            signal = -0.5
        elif current < avg_12m - 0.3:
            signal = 1.0
        else:
            signal = 0.0

        return {
            "signal": signal,
            "value": current,
            "label": f"Unemployment: {current:.1f}% (Sahm: {sahm:+.2f})",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"Unemployment: error ({e})"}


def get_fed_funds_signal():
    """Fed Funds Rate trajectory.
    Rate cuts = bullish. Rate hikes = bearish."""
    try:
        ff = _fetch_fred_series("FEDFUNDS")
        if ff.empty or len(ff) < 3:
            return {"signal": 0, "value": None, "label": "Fed Funds: N/A"}

        current = ff.iloc[-1]
        prev_3m = ff.iloc[-3] if len(ff) >= 3 else ff.iloc[0]
        prev_6m = ff.iloc[-6] if len(ff) >= 6 else ff.iloc[0]

        change_3m = current - prev_3m
        change_6m = current - prev_6m

        if change_6m < -0.5:
            signal = 1.0   # aggressive cuts
        elif change_3m < -0.25:
            signal = 0.5   # cutting
        elif change_3m > 0.25:
            signal = -0.5  # hiking
        elif change_6m > 0.5:
            signal = -1.0  # aggressive hikes
        else:
            signal = 0.0   # on hold

        # Direction string uses the same 6m horizon as the signal — old
        # version used change_3m which could disagree with the signal
        # (e.g., 6m aggressive cuts but most recent 3m flat → label said
        # "holding" while signal said bullish).
        direction = ("cutting" if change_6m < 0
                     else "hiking" if change_6m > 0
                     else "holding")
        return {
            "signal": signal,
            "value": current,
            "label": f"Fed Funds: {current:.2f}% ({direction}, Δ6m: {change_6m:+.2f})",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"Fed Funds: error ({e})"}


def get_financial_conditions_signal():
    """Chicago Fed National Financial Conditions Index.
    Positive = tightening = bearish. Negative = loose = bullish."""
    try:
        nfci = _fetch_fred_series("NFCI")
        if nfci.empty:
            return {"signal": 0, "value": None, "label": "NFCI: N/A"}

        current = nfci.iloc[-1]
        if current > 0.5:
            signal = -1.0
        elif current > 0:
            signal = -0.5
        elif current > -0.5:
            signal = 0.5
        else:
            signal = 1.0

        return {
            "signal": signal,
            "value": current,
            "label": f"NFCI: {current:.3f} ({'tight' if current > 0 else 'loose'})",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"NFCI: error ({e})"}


def get_market_breadth_signal():
    """Market breadth via S&P 500 distance from 200-day SMA.
    Proxy for market-wide trend health."""
    try:
        sp = _fetch_fred_series("SP500")
        if sp.empty or len(sp) < 200:
            return {"signal": 0, "value": None, "label": "Breadth: N/A"}

        current = sp.iloc[-1]
        # rolling(200).mean().iloc[-1] is the correct "200-day SMA at the
        # latest point" — sp.iloc[-200:].mean() happened to coincide for
        # a clean daily series but isn't equivalent under any missing-day
        # interpolation FRED might do.
        sma200 = sp.rolling(200).mean().iloc[-1]
        if pd.isna(sma200):
            return {"signal": 0, "value": None, "label": "Breadth: insufficient history"}
        pct_above = (current / sma200 - 1) * 100

        if pct_above > 10:
            signal = 1.0
        elif pct_above > 3:
            signal = 0.5
        elif pct_above > -3:
            signal = 0.0
        elif pct_above > -10:
            signal = -0.5
        else:
            signal = -1.0

        return {
            "signal": signal,
            "value": pct_above,
            "label": f"S&P vs 200SMA: {pct_above:+.1f}%",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"Breadth: error ({e})"}


# ── Composite ────────────────────────────────────────────────────

INDICATOR_WEIGHTS = {
    "yield_curve":     0.25,   # strongest recession predictor
    "credit_spreads":  0.20,   # financial stress
    "unemployment":    0.15,   # labor market
    "fed_funds":       0.15,   # monetary policy
    "financial_cond":  0.15,   # overall conditions
    "market_breadth":  0.10,   # trend confirmation
}


def macro_regime_score():
    """Compute composite macro regime score.

    Returns:
      score: float from -1 (very bearish) to +1 (very bullish)
      details: list of individual indicator results
      regime: str — 'expansion', 'late-cycle', 'contraction', 'recovery'
    """
    indicators = {
        "yield_curve":    get_yield_curve_signal(),
        "credit_spreads": get_credit_spread_signal(),
        "unemployment":   get_unemployment_signal(),
        "fed_funds":      get_fed_funds_signal(),
        "financial_cond": get_financial_conditions_signal(),
        "market_breadth": get_market_breadth_signal(),
    }

    # Normalize over only the indicators that actually returned data.
    # Failed indicators (result["value"] is None) used to contribute 0 to
    # the numerator but still consumed weight in the denominator → score
    # got pulled toward 0 whenever FRED was partially down, which the
    # macro_risk_adjustment caller would misread as "regime turning neutral"
    # and reduce equity exposure for no real reason.
    composite = 0.0
    total_weight = 0.0
    for name, result in indicators.items():
        if result.get("value") is None:
            continue
        w = INDICATOR_WEIGHTS.get(name, 0.1)
        composite += result["signal"] * w
        total_weight += w

    if total_weight > 0:
        composite /= total_weight

    # Classify regime
    if composite > 0.3:
        regime = "expansion"
    elif composite > 0:
        regime = "late-cycle"
    elif composite > -0.3:
        regime = "early-contraction"
    else:
        regime = "contraction"

    return {
        "score": composite,
        "regime": regime,
        "indicators": indicators,
    }


def macro_composite_score() -> float:
    """Raw composite macro score in [-1, +1] without the risk-adjustment mapping.

    Used by baseline.py to capture the macro regime at plan time and by
    breakers.py to detect intra-day regime flips.
    """
    return float(macro_regime_score()["score"])


def macro_risk_adjustment(base_equity_pct: float) -> float:
    """Adjust equity allocation based on macro regime + political risk.

    Blends the FRED macro score (70%) with the political-risk score (30%).
    Linear scaling per source: score 1.0 → 100% of target, score -1.0 → 40%.
    Caps are defensive — scores are already bounded [-1, +1] upstream.
    Falls back to FRED-only if the political-forecast module is unavailable.
    """
    result = macro_regime_score()
    score = result["score"]
    fred_adj = max(0.4, min(1.0, 0.7 + 0.3 * score))
    try:
        from quant.news.forecast import get_latest_political_score
        political_score = get_latest_political_score()
        political_adj = max(0.4, min(1.0, 0.7 + 0.3 * political_score))
        blended = 0.7 * fred_adj + 0.3 * political_adj
    except Exception:
        blended = fred_adj  # FRED-only fallback
    return base_equity_pct * blended


# ── CLI: force-refresh all FRED cache ────────────────────────────

_REFRESH_SERIES = (
    "DGS10", "DGS2", "FEDFUNDS", "BAMLH0A0HYM2", "UNRATE",
    "NFCI", "SP500",
)


def force_refresh_all() -> dict:
    """Re-fetch every FRED series the composite consumes, bypassing the
    12h cache. Use after a Fed announcement / data print to make sure the
    next macro_regime_score reads fresh values rather than stale cache."""
    results: dict = {}
    for series_id in _REFRESH_SERIES:
        try:
            s = _fetch_fred_series(series_id, force=True)
            results[series_id] = "OK" if not s.empty else "empty"
        except Exception as e:
            results[series_id] = f"error: {e}"
    return results


def main():
    import sys
    if "--refresh" in sys.argv[1:]:
        print("Forcing FRED cache refresh...")
        for sid, status in force_refresh_all().items():
            print(f"  {sid:18s} {status}")
        return
    # Default: print current regime
    result = macro_regime_score()
    print(f"Macro regime: {result['regime'].upper()} "
          f"(score: {result['score']:+.3f})")
    for name, ind in result["indicators"].items():
        print(f"  {name:18s} {ind['signal']:+.1f}  {ind['label']}")


if __name__ == "__main__":
    main()
