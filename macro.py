"""
Macro Regime Detection using FRED (Federal Reserve Economic Data)

Uses leading economic indicators to determine if the macro environment
favors risk assets or defensive positioning. This overlays on top of
the momentum strategy to improve timing.

Key indicators:
  1. Yield Curve (10Y-2Y spread) — inverted = recession warning
  2. Credit Spreads (BAA-AAA) — widening = stress
  3. Unemployment Claims — rising = slowdown
  4. ISM Manufacturing PMI — below 50 = contraction
  5. Fed Funds Rate trajectory — tightening vs easing
  6. CPI YoY — inflation regime
  7. Financial Conditions — Chicago Fed NFCI

Each indicator produces a score from -1 (bearish) to +1 (bullish).
The composite score drives portfolio risk adjustment.
"""
import os
import json
import time
import datetime as dt
import pandas as pd
import numpy as np

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Load from .env file
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# FRED series IDs
SERIES = {
    "yield_10y":    "DGS10",          # 10-Year Treasury
    "yield_2y":     "DGS2",           # 2-Year Treasury
    "yield_3m":     "DGS3MO",         # 3-Month Treasury
    "fed_funds":    "FEDFUNDS",       # Federal Funds Rate
    "baa_yield":    "DBAA",           # Moody's BAA Corporate
    "aaa_yield":    "DAAA",           # Moody's AAA Corporate
    "unemployment": "UNRATE",         # Unemployment Rate
    "claims":       "ICSA",           # Initial Jobless Claims
    "cpi_yoy":      "CPIAUCSL",       # CPI (we compute YoY)
    "ism_mfg":      "MANEMP",         # Manufacturing Employment (proxy)
    "nfci":         "NFCI",           # Chicago Fed Financial Conditions
    "m2":           "M2SL",           # M2 Money Supply
    "sp500":        "SP500",          # S&P 500
}


def _fetch_fred_series(series_id, start="2020-01-01"):
    """Fetch a single FRED series. Uses fredapi if API key available,
    otherwise falls back to yfinance for market data."""
    cache_path = os.path.join(CACHE_DIR, f"fred_{series_id}.csv")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < 12 * 3600:  # 12-hour cache
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            return df.iloc[:, 0]

    if FRED_API_KEY:
        from fredapi import Fred
        fred = Fred(api_key=FRED_API_KEY)
        data = fred.get_series(series_id, observation_start=start)
        data = data.dropna()
        data.to_frame(series_id).to_csv(cache_path)
        return data
    else:
        # No FRED key — use yfinance as fallback for key rates
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
            s.to_frame(series_id).to_csv(cache_path)
            return s

    return pd.Series(dtype=float)


def get_yield_curve_signal():
    """Yield curve: 10Y-2Y spread.
    Inverted (<0) = strong recession signal = bearish.
    Steep (>1.5) = early expansion = bullish."""
    try:
        y10 = _fetch_fred_series("DGS10")
        y2 = _fetch_fred_series("DGS2")
        if y10.empty or y2.empty:
            # Fallback: use yfinance for treasury yields
            y10 = _fetch_fred_series("DGS10")
            if y10.empty:
                return {"signal": 0, "value": None, "label": "N/A"}
            spread = y10.iloc[-1] - 4.0  # rough estimate
        else:
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
    """Credit spread: BAA-AAA.
    Widening = stress = bearish. Tightening = calm = bullish."""
    try:
        baa = _fetch_fred_series("DBAA")
        aaa = _fetch_fred_series("DAAA")
        if baa.empty or aaa.empty:
            return {"signal": 0, "value": None, "label": "Credit spreads: N/A"}

        spread = baa.iloc[-1] - aaa.iloc[-1]
        # Historical median ~1.0%, stress >2%
        if spread > 2.5:
            signal = -1.0
        elif spread > 1.8:
            signal = -0.5
        elif spread > 1.2:
            signal = 0.0
        elif spread > 0.8:
            signal = 0.5
        else:
            signal = 1.0

        return {
            "signal": signal,
            "value": spread,
            "label": f"BAA-AAA Spread: {spread:.2f}%",
        }
    except Exception as e:
        return {"signal": 0, "value": None, "label": f"Credit spreads: error ({e})"}


def get_unemployment_signal():
    """Unemployment rate trend.
    Rising = bearish. Falling = bullish."""
    try:
        unemp = _fetch_fred_series("UNRATE")
        if unemp.empty or len(unemp) < 6:
            return {"signal": 0, "value": None, "label": "Unemployment: N/A"}

        current = unemp.iloc[-1]
        avg_6m = unemp.iloc[-6:].mean()
        avg_12m = unemp.iloc[-12:].mean() if len(unemp) >= 12 else avg_6m

        # Sahm Rule inspired: if 3-month avg rises 0.5% above 12-month low
        low_12m = unemp.iloc[-12:].min() if len(unemp) >= 12 else unemp.min()
        sahm = avg_6m - low_12m

        if sahm > 0.5:
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

        direction = "cutting" if change_3m < 0 else "hiking" if change_3m > 0 else "holding"
        return {
            "signal": signal,
            "value": current,
            "label": f"Fed Funds: {current:.2f}% ({direction}, Δ3m: {change_3m:+.2f})",
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
        sma200 = sp.iloc[-200:].mean()
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

    composite = 0
    total_weight = 0
    for name, result in indicators.items():
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
    """Adjust equity allocation based on macro regime.

    In expansion: hold full equity allocation.
    In contraction: reduce to 50% of target.
    """
    result = macro_regime_score()
    score = result["score"]

    # Linear scaling: score 1.0 → 100% of target, score -1.0 → 40% of target
    adj = 0.7 + 0.3 * score  # ranges from 0.4 to 1.0
    adj = max(0.4, min(1.0, adj))

    return base_equity_pct * adj
