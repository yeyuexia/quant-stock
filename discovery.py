#!/usr/bin/env python3
"""
Auto Stock Discovery — refreshes the candidate watchlist by scanning the
market through deterministic, ranked sources.

Pipeline:
  1. Gather candidates from prioritized sources (current watchlist,
     smart-money signals from quant/data_sources, S&P 500 round-robin,
     and optional Reddit trending).
  2. Batch-fetch 6mo OHLCV via data.fetch_prices/fetch_ohlcv (one HTTP
     burst), then ThreadPool-parallel info + fundamentals per ticker.
  3. Score every dimension cross-sectionally (rank percentile) and
     combine with config.DISCOVERY_WEIGHTS.
  4. Rank, tag, optionally append top names to watchlist_auto.json
     (append-only; config.py is never rewritten) and append a one-line
     audit record to .cache/discovery.log.

Auto-discovered tickers live in watchlist_auto.json (config.WATCHLIST_AUTO_PATH).
config.py loads that file and unions it with the hand-curated WATCHLIST_SEED to
form config.WATCHLIST. --update / --prune touch ONLY the JSON file, so the seed
block's hand-written comments are preserved.

Usage:
  python3 discovery.py                 # full scan, print results
  python3 discovery.py --trending      # quick: just list smart-money + reddit tickers
  python3 discovery.py --update        # scan + append top 50 to watchlist_auto.json
  python3 discovery.py --include-reddit  # include Reddit-trending as a source
  python3 discovery.py --prune         # list watchlist names not seen in N days
  python3 discovery.py --prune --confirm # remove stale AUTO names from watchlist_auto.json
"""
from __future__ import annotations
import sys
import os
import csv
import io
import json
import time
import re
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

import numpy as np
import pandas as pd

import config
import data as data_mod

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

DISCOVERY_LOG = os.path.join(CACHE_DIR, "discovery.log")
SP500_POINTER = os.path.join(CACHE_DIR, "disc_sp500_pointer.json")
LASTPASS_PATH = os.path.join(CACHE_DIR, "discovery_lastpass.json")


# ── Tiny disk cache (kept for SP500 list / pointer) ─────────────

def _cache_get(key: str, ttl_hours: float = 12):
    path = os.path.join(CACHE_DIR, f"disc_{key}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl_hours * 3600:
        with open(path) as f:
            return json.load(f)
    return None


def _cache_set(key: str, payload):
    path = os.path.join(CACHE_DIR, f"disc_{key}.json")
    with open(path, "w") as f:
        json.dump(payload, f, default=str)


# ── Candidate sources ───────────────────────────────────────────

def get_sp500_tickers() -> List[str]:
    """Full S&P 500 list from Wikipedia (1-week cache)."""
    cached = _cache_get("sp500", ttl_hours=168)
    if cached:
        return cached
    try:
        import urllib.request
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode()
        tickers = re.findall(r'<a[^>]*class="external text"[^>]*>([A-Z][A-Z.-]{0,4})</a>', html)
        tickers = [t for t in tickers if t.replace(".", "").replace("-", "").isalpha()]
        tickers = list(dict.fromkeys(tickers))
        if len(tickers) > 100:
            _cache_set("sp500", tickers)
            return tickers
    except Exception:
        pass
    return []


def parse_ishares_holdings_csv(text: str) -> List[str]:
    """Extract equity tickers from an iShares full-holdings CSV.

    The file has a ~9-line preamble before a header row that starts with
    "Ticker". We DictReader from that row and keep rows whose Asset Class is
    "Equity", filtering to valid US-equity-style symbols. Returns de-duped,
    order-preserved tickers; [] on any structural problem (fail-open).
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip('"').startswith("Ticker"):
            start = i
            break
    if start is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out: List[str] = []
    for row in reader:
        if (row.get("Asset Class") or "").strip() != "Equity":
            continue
        t = (row.get("Ticker") or "").strip().upper()
        if t and t.replace(".", "").replace("-", "").isalpha() and len(t) <= 5:
            out.append(t)
    return list(dict.fromkeys(out))


def fetch_etf_full_holdings(symbol: str, url: str) -> List[str]:
    """Download one ETF's full-holdings CSV and parse out equity tickers.
    Never raises — returns [] on any network/parse failure."""
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
        return parse_ishares_holdings_csv(text)
    except Exception:
        return []


def get_universe_tickers() -> List[str]:
    """Discovery scan universe = union of config.DISCOVERY_UNIVERSE_ETFS holdings,
    cached 1 week. Falls back to the Wikipedia S&P 500 list if the CSV download
    yields too few names (so discovery degrades gracefully, never to empty)."""
    cached = _cache_get("universe", ttl_hours=168)
    if cached:
        return cached[: config.DISCOVERY_UNIVERSE_MAX]
    tickers: List[str] = []
    for sym, url in config.DISCOVERY_UNIVERSE_ETFS.items():
        tickers.extend(fetch_etf_full_holdings(sym, url))
    tickers = list(dict.fromkeys(tickers))
    if len(tickers) >= 200:
        _cache_set("universe", tickers)
        return tickers[: config.DISCOVERY_UNIVERSE_MAX]
    # Fallback: keep discovery working even if iShares blocks the download.
    return get_sp500_tickers()[: config.DISCOVERY_UNIVERSE_MAX]


def sp500_round_robin_slice(batch_size: int) -> List[str]:
    """Return the next `batch_size` S&P 500 tickers, advancing the pointer.

    Wraps around when the end is reached. Pointer lives in .cache/disc_sp500_pointer.json.
    """
    universe = get_sp500_tickers()
    if not universe:
        return []
    try:
        with open(SP500_POINTER) as f:
            pos = int(json.load(f).get("pos", 0))
    except (FileNotFoundError, json.JSONDecodeError):
        pos = 0
    pos %= len(universe)
    end = pos + batch_size
    if end <= len(universe):
        out = universe[pos:end]
    else:
        out = universe[pos:] + universe[: end - len(universe)]
    new_pos = end % len(universe)
    with open(SP500_POINTER, "w") as f:
        json.dump({"pos": new_pos, "updated": dt.datetime.utcnow().isoformat()}, f)
    return out


def get_smart_money_tickers(sources: tuple = None) -> dict:
    """Harvest tickers from quant/data_sources external signals.

    Returns {ticker: [source1, source2, ...]} so we know which feeds each came from.
    Sources requested are limited to config.DISCOVERY_TICKER_SOURCES (default
    excludes "reddit" — that's gated behind --include-reddit at the caller).
    """
    wanted = set(sources or config.DISCOVERY_TICKER_SOURCES)
    out: dict = {}
    try:
        from quant.data_sources import fetch_all_externals
        signals = fetch_all_externals()
    except Exception:
        return out
    for sig in signals:
        if sig.source not in wanted or sig.error or not sig.data:
            continue
        for row in sig.data:
            if not isinstance(row, dict):
                continue
            t = (row.get("ticker") or "").strip().upper()
            if t and t.replace(".", "").replace("-", "").isalpha() and len(t) <= 5:
                out.setdefault(t, []).append(sig.source)
    return out


def get_reddit_trending_tickers(top_n: int = 30) -> List[str]:
    """Reddit-mentioned tickers, weighted by engagement (separate from smart-money)."""
    try:
        from quant.data_sources import fetch_reddit_trending
        sig = fetch_reddit_trending()
        if sig.error or not sig.data:
            return []
        rows = sorted(sig.data, key=lambda r: r.get("mentions", 0), reverse=True)
        return [r["ticker"] for r in rows[:top_n] if r.get("ticker")]
    except Exception:
        return []


def merge_candidates(
    *,
    include_reddit: bool = False,
    max_scan: Optional[int] = None,
) -> tuple[list, dict]:
    """Deterministic, priority-ordered candidate merge.

    Order: current WATCHLIST → smart-money → Reddit (opt-in) → full scan universe
    (config.DISCOVERY_UNIVERSE_ETFS via get_universe_tickers; Russell 1000 today).
    Returns (ordered_tickers, source_map). Each ticker appears at most once.
    """
    cap = max_scan or config.DISCOVERY_UNIVERSE_MAX
    ordered: list = []
    sources: dict = {}

    def _add(ticker: str, source: str):
        if ticker in sources:
            sources[ticker].append(source)
            return
        sources[ticker] = [source]
        ordered.append(ticker)

    for t in config.WATCHLIST:
        _add(t, "watchlist")

    sm = get_smart_money_tickers()
    for t, feeds in sm.items():
        for f in feeds:
            _add(t, f)

    if include_reddit:
        for t in get_reddit_trending_tickers():
            _add(t, "reddit")

    # Bulk universe — fills the rest up to the cap.
    for t in get_universe_tickers():
        if len(ordered) >= cap:
            break
        _add(t, "universe")

    return ordered[:cap], sources


# ── Per-ticker fundamentals + price snapshot (parallel) ─────────

def _safe_float(v):
    try:
        f = float(v)
        if f != f or abs(f) == float("inf"):
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_ticker_snapshot(ticker: str) -> Optional[dict]:
    """One ticker: info + fundamentals via data.py cached helpers.

    Returns None if non-equity, micro-cap (<$300M), or US filter rejects.
    Prices/RS/IPO age/distance are filled later from the batch download.
    """
    try:
        info = data_mod.fetch_info(ticker)
    except Exception:
        return None
    if not info or info.get("quoteType") != "EQUITY":
        return None
    mcap = _safe_float(info.get("marketCap")) or 0.0
    if mcap < 300e6:
        return None
    country = info.get("country") or ""
    if config.DISCOVERY_REQUIRE_US and country and country != "United States":
        return None

    fund = {}
    try:
        fund = data_mod.fetch_fundamentals(ticker) or {}
    except Exception:
        pass

    first_trade = info.get("firstTradeDateEpochUtc")
    ipo_age_years = None
    if first_trade:
        try:
            ipo_age_years = (time.time() - float(first_trade)) / (365.25 * 86400)
        except (TypeError, ValueError):
            ipo_age_years = None

    return {
        "ticker": ticker,
        "name": (info.get("shortName") or ticker)[:25],
        "price": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
        "market_cap": mcap,
        "market_cap_B": mcap / 1e9,
        "pe": _safe_float(info.get("trailingPE") or info.get("forwardPE")),
        "roe": _safe_float(info.get("returnOnEquity")),
        "debt_equity": (_safe_float(info.get("debtToEquity")) or 0) / 100 if info.get("debtToEquity") else None,
        "rev_growth": _safe_float(info.get("revenueGrowth")) or 0,
        "div_yield": _safe_float(info.get("dividendYield")) or 0,
        "avg_volume": _safe_float(info.get("averageVolume")) or 0,
        "sector": info.get("sector") or "",
        "country": country,
        "ipo_age_years": ipo_age_years,
        "eps_q_growth": fund.get("eps_q_growth"),
        "quarterly_eps": fund.get("quarterly_eps", []),
    }


def fetch_snapshots_parallel(tickers: List[str], workers: Optional[int] = None) -> List[dict]:
    """Concurrent per-ticker info + fundamentals fetch. Returns list ordered by input."""
    if not tickers:
        return []
    w = workers or config.DISCOVERY_THREAD_WORKERS
    results: dict = {}
    with ThreadPoolExecutor(max_workers=w) as ex:
        futures = {ex.submit(fetch_ticker_snapshot, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                snap = fut.result()
            except Exception:
                snap = None
            if snap:
                results[t] = snap
    return [results[t] for t in tickers if t in results]


def enrich_with_prices(snapshots: List[dict]) -> List[dict]:
    """Single batched price download, then compute RS / momentum / 52w-high / SMA50 distance."""
    if not snapshots:
        return snapshots
    tickers = [s["ticker"] for s in snapshots]
    try:
        closes = data_mod.fetch_prices(tickers, period="1y")
    except Exception:
        closes = pd.DataFrame()
    if closes.empty:
        for s in snapshots:
            s.update({"ret_3m": None, "rs_pct": None,
                      "dist_52w_high": None, "sma50_dist_pct": None})
        return snapshots

    # RS percentile = 0.4 × 3M + 0.3 × 6M + 0.3 × 12M return, ranked across universe.
    def _ret(n):
        if len(closes) >= n:
            return closes.iloc[-1] / closes.iloc[-n] - 1
        return pd.Series(np.nan, index=closes.columns)
    composite = (
        0.40 * _ret(63).fillna(0)
        + 0.30 * _ret(126).fillna(0)
        + 0.30 * _ret(252).fillna(0)
    )
    rs_pct = composite.rank(pct=True) * 100

    for s in snapshots:
        t = s["ticker"]
        if t not in closes.columns:
            s.update({"ret_3m": None, "rs_pct": None,
                      "dist_52w_high": None, "sma50_dist_pct": None})
            continue
        ser = closes[t].dropna()
        last = float(ser.iloc[-1]) if not ser.empty else None
        s["ret_3m"] = float(ser.iloc[-1] / ser.iloc[-63] - 1) if len(ser) >= 63 else None
        s["rs_pct"] = float(rs_pct.get(t, np.nan)) if not pd.isna(rs_pct.get(t, np.nan)) else None
        s["dist_52w_high"] = (
            float(last / ser.max() - 1) if last and ser.max() > 0 else None
        )
        if len(ser) >= 50 and last:
            sma50 = float(ser.rolling(50).mean().iloc[-1])
            s["sma50_dist_pct"] = (last - sma50) / sma50 if sma50 > 0 else None
        else:
            s["sma50_dist_pct"] = None
    return snapshots


# ── Stage-1 prescreen: universe-wide RS + liquidity gate ────────

def _chunked_ohlcv(tickers: List[str], period: str = "1y", chunk: int = 150) -> pd.DataFrame:
    """Batch-download OHLCV in chunks (yfinance chokes on 1000+ tickers at once),
    concatenated to one MultiIndex (field, ticker) frame. Empty on total failure."""
    frames = []
    for i in range(0, len(tickers), chunk):
        part = data_mod.fetch_ohlcv(tickers[i:i + chunk], period=period)
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def prescreen_universe(tickers: List[str], protected: set, *, keep: Optional[int] = None):
    """Stage 1 — cheap, batched. Rank the whole universe on universe-wide relative
    strength, apply a price/liquidity gate, and carry the top `keep` survivors
    (plus all `protected` names) into Stage 2.

    Returns (survivors, metrics) where metrics[ticker] = {rs_pct, ret_3m,
    dist_52w_high, sma50_dist_pct, price, avg_dollar_vol} so Stage 2 need not
    recompute prices. Fail-open: if no price data, carry protected + the raw head
    so a network blip never empties discovery.
    """
    keep = keep or config.DISCOVERY_STAGE1_KEEP
    ohlcv = _chunked_ohlcv(tickers, period="1y")
    if ohlcv.empty:
        survivors = list(dict.fromkeys(list(protected) + tickers))[: max(keep, len(protected))]
        return survivors, {}

    close = ohlcv["Close"]
    vol = ohlcv["Volume"]

    def _ret(n):
        return close.iloc[-1] / close.iloc[-n] - 1 if len(close) >= n else pd.Series(np.nan, index=close.columns)

    rs_pct = (0.40 * _ret(63).fillna(0) + 0.30 * _ret(126).fillna(0)
              + 0.30 * _ret(252).fillna(0)).rank(pct=True) * 100

    metrics: dict = {}
    for t in close.columns:
        ser = close[t].dropna()
        if ser.empty:
            continue
        last = float(ser.iloc[-1])
        vser = vol[t].dropna() if t in vol.columns else pd.Series(dtype=float)
        avg_dollar_vol = float((ser * vser).tail(20).mean()) if not vser.empty else 0.0
        sma50 = ser.rolling(50).mean().iloc[-1] if len(ser) >= 50 else None
        metrics[t] = {
            "price": last,
            "rs_pct": float(rs_pct.get(t)) if not pd.isna(rs_pct.get(t, np.nan)) else None,
            "ret_3m": float(ser.iloc[-1] / ser.iloc[-63] - 1) if len(ser) >= 63 else None,
            "dist_52w_high": float(last / ser.max() - 1) if ser.max() > 0 else None,
            "sma50_dist_pct": (float((last - sma50) / sma50) if sma50 and sma50 > 0 else None),
            "avg_dollar_vol": avg_dollar_vol,
        }

    def _passes_gate(t):
        m = metrics.get(t)
        if not m:
            return False
        return (m["price"] or 0) >= config.DISCOVERY_MIN_PRICE and \
               m["avg_dollar_vol"] >= config.DISCOVERY_MIN_DOLLAR_VOLUME

    gated = [t for t in metrics if _passes_gate(t)]
    gated.sort(key=lambda t: (metrics[t]["rs_pct"] or 0.0), reverse=True)
    survivors = gated[:keep]
    # Protected names always advance, even if illiquid or price-less.
    survivors = list(dict.fromkeys(survivors + [t for t in protected]))
    return survivors, metrics


# ── EPS acceleration (CANSLIM-aligned) ──────────────────────────

def _eps_acceleration_score(quarterly_eps: list) -> float:
    """0-1 score on whether the most-recent QoQ growth exceeds the prior QoQ growth.

    Matches screener._eps_acceleration in spirit but yields a float so it can be
    cross-section-ranked. Returns 0.0 when undefined.
    """
    if not quarterly_eps or len(quarterly_eps) < 3:
        return 0.0
    q0, q1, q2 = quarterly_eps[0], quarterly_eps[1], quarterly_eps[2]
    if not q1 or not q2:
        return 0.0
    g1 = (q0 - q1) / abs(q1)
    g2 = (q1 - q2) / abs(q2)
    if g1 <= 0 or g1 <= g2:
        return 0.0
    return min(1.0, g1 - g2)  # margin of acceleration


# ── Composite scoring (cross-section rank) ──────────────────────

def _pct_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """Percentile rank 0-100 with NaN-safe fail-open (NaN → 50, the neutral midpoint)."""
    ranked = series.rank(pct=True, ascending=ascending, na_option="keep") * 100
    return ranked.fillna(50.0)


def compute_composite_scores(df: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
    """Cross-sectional rank for each dimension, weighted sum to `composite_score`."""
    if df.empty:
        df["composite_score"] = pd.Series(dtype=float)
        return df
    w = weights or config.DISCOVERY_WEIGHTS

    pct = pd.DataFrame(index=df.index)
    pct["rs"]            = _pct_rank(df.get("rs_pct"))
    pct["rev_growth"]    = _pct_rank(df.get("rev_growth"))
    pct["eps_q_growth"]  = _pct_rank(df.get("eps_q_growth"))
    pct["roe"]           = _pct_rank(df.get("roe"))
    pct["mom_3m"]        = _pct_rank(df.get("ret_3m"))
    # closer to 52w high is BETTER — dist is ≤ 0, so larger (less negative) ranks higher
    pct["dist_52w_high"] = _pct_rank(df.get("dist_52w_high"))
    # younger IPOs rank higher: invert (ascending=False)
    pct["ipo_age"]       = _pct_rank(df.get("ipo_age_years"), ascending=False)
    pct["sma50_dist"]    = _pct_rank(df.get("sma50_dist_pct"))
    # PE: only score POSITIVE P/E names; loss-makers excluded from this dimension
    pe = df.get("pe")
    inv_pe = pd.Series(np.where((pe > 0) & pe.notna(), 1.0 / pe, np.nan), index=df.index)
    pct["value_pe"]      = _pct_rank(inv_pe)
    # EPS acceleration is a separate boost on top of the weighted sum
    eps_accel = df.get("quarterly_eps").apply(_eps_acceleration_score)

    total_w = sum(w.values())
    score = sum(pct[k] * w[k] for k in w if k in pct.columns) / max(total_w, 1e-9)
    # Acceleration bonus: up to +5 percentile points
    df["composite_score"] = (score + 5.0 * eps_accel).round(2)
    df["eps_accel_score"] = eps_accel
    for k in pct.columns:
        df[f"rank_{k}"] = pct[k].round(1)
    return df


# ── Hard tier criteria (still useful for the display tags) ──────

SCREEN_CRITERIA = {
    "growth": {
        "description": "High revenue growth, momentum names",
        "min_rev_growth": 0.15,
        "min_market_cap": 1e9,
        "max_market_cap": 100e9,
    },
    "value": {
        "description": "Undervalued with decent quality",
        "max_pe": 25,
        "min_roe": 0.10,
        "min_market_cap": 2e9,
    },
    "smid_momentum": {
        "description": "Small/mid-cap with strong price momentum",
        "min_rev_growth": 0.10,
        "min_rs_pct": 75,
        "min_market_cap": 500e6,
        "max_market_cap": 30e9,
    },
    "quality_dividend": {
        "description": "High ROE, pays dividend, stable",
        "min_roe": 0.15,
        "min_div_yield": 0.01,
        "max_debt_equity": 2.0,
        "min_market_cap": 5e9,
    },
}

# Constraints where missing data should REJECT (fail-closed).
# Everything else is fail-open (consistent with how downstream CANSLIM behaves).
_FAIL_CLOSED_KEYS = {"max_pe", "min_roe", "min_div_yield", "max_debt_equity"}


def passes_criteria(stock: dict, criteria: dict) -> bool:
    """Hard-gate check. Value/quality keys are fail-closed; growth/momentum fail-open."""
    def _val(k):
        return stock.get(k)

    def _need(field_key, criterion_key):
        return criterion_key in criteria and criteria[criterion_key] is not None

    # Market cap (always strict — we know it because we filtered micro-caps already)
    if _need("market_cap", "min_market_cap"):
        v = _val("market_cap")
        if v is None or v < criteria["min_market_cap"]:
            return False
    if _need("market_cap", "max_market_cap"):
        v = _val("market_cap")
        if v is None or v > criteria["max_market_cap"]:
            return False

    # Value/quality — fail-closed
    if _need("pe", "max_pe"):
        v = _val("pe")
        if v is None or v <= 0 or v > criteria["max_pe"]:
            return False
    if _need("roe", "min_roe"):
        v = _val("roe")
        if v is None or v < criteria["min_roe"]:
            return False
    if _need("debt_equity", "max_debt_equity"):
        v = _val("debt_equity")
        if v is None or v > criteria["max_debt_equity"]:
            return False
    if _need("div_yield", "min_div_yield"):
        v = _val("div_yield")
        if v is None or v < criteria["min_div_yield"]:
            return False

    # Growth/momentum — fail-open (missing => assume OK; downstream CANSLIM re-checks)
    if _need("rev_growth", "min_rev_growth"):
        v = _val("rev_growth")
        if v is not None and v < criteria["min_rev_growth"]:
            return False
    if _need("rs_pct", "min_rs_pct"):
        v = _val("rs_pct")
        if v is not None and v < criteria["min_rs_pct"]:
            return False
    return True


# ── Main discovery pipeline ─────────────────────────────────────

def discover(
    max_scan: Optional[int] = None,
    include_reddit: bool = False,
    verbose: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """Two-stage discovery pipeline. Returns (ranked_df, source_map).

    Stage 1 (cheap): rank the whole universe on universe-wide RS + liquidity,
    keep the top DISCOVERY_STAGE1_KEEP survivors (+ watchlist/smart-money, which
    are 'protected'). Stage 2 (expensive): fetch info+fundamentals only for
    survivors, then compute the peer-relative composite.
    """
    if verbose:
        print("  Gathering candidates...")
    candidates, sources = merge_candidates(include_reddit=include_reddit, max_scan=max_scan)

    # Anything sourced by something other than the bulk universe is protected:
    # never dropped by the Stage-1 liquidity/RS gate.
    protected = {t for t in candidates if any(s != "universe" for s in sources.get(t, []))}

    if verbose:
        print(f"    {len(candidates)} candidates; Stage 1 ranking on price/liquidity...")
    survivors, price_metrics = prescreen_universe(candidates, protected)
    if verbose:
        print(f"    {len(survivors)} survivors → Stage 2 (info + fundamentals)")

    snaps = fetch_snapshots_parallel(survivors)
    # Attach Stage-1 price metrics (no second price download).
    for s in snaps:
        m = price_metrics.get(s["ticker"], {})
        for k in ("ret_3m", "rs_pct", "dist_52w_high", "sma50_dist_pct"):
            s[k] = m.get(k)

    df = pd.DataFrame(snaps)
    if df.empty:
        return df, sources
    if "quarterly_eps" not in df.columns:
        df["quarterly_eps"] = [[] for _ in range(len(df))]

    df = compute_composite_scores(df)
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    df["sources"] = df["ticker"].map(lambda t: ",".join(sources.get(t, [])))

    for name, crit in SCREEN_CRITERIA.items():
        df[f"pass_{name}"] = df.apply(lambda r: passes_criteria(r.to_dict(), crit), axis=1)
    return df, sources


def generate_watchlist(df: pd.DataFrame, max_per_category: int = 8) -> dict:
    """Categorized watchlist for display."""
    out = {cat: [] for cat in SCREEN_CRITERIA}
    for cat in out:
        col = f"pass_{cat}"
        if col in df.columns:
            out[cat] = df[df[col]].head(max_per_category)["ticker"].tolist()
    out["top_overall"] = df.head(15)["ticker"].tolist()
    return out


def _load_auto_file() -> list:
    """Read the current auto-discovered ticker list (order preserved, [] on miss)."""
    return config._load_auto_watchlist()


def _write_auto_file(tickers: List[str]) -> None:
    """Atomically (tmp file + os.replace) write the auto-discovered ticker list.

    Writes config.WATCHLIST_AUTO_PATH — config.py is NEVER touched.
    """
    path = config.WATCHLIST_AUTO_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(tickers, f, indent=2)
    os.replace(tmp, path)


def update_config_watchlist(new_tickers: List[str]) -> list:
    """Append new tickers to watchlist_auto.json (append-only, dedupe, order kept).

    config.py is NEVER modified — the hand-curated WATCHLIST_SEED block (and its
    comments) is left intact. Returns the full seed ∪ auto union so callers can
    report the effective watchlist size.
    """
    current = _load_auto_file()
    combined_auto = list(dict.fromkeys(current + list(new_tickers)))
    _write_auto_file(combined_auto)
    return config._union_watchlist(config.WATCHLIST_SEED, combined_auto)


def prune_stale_from_config(stale_tickers: List[str]) -> tuple[list, list, list]:
    """Remove `stale_tickers` from watchlist_auto.json ONLY.

    A stale name that is NOT in the auto file but IS a hand-curated seed name
    is skipped (never removed from config.py) and reported separately.

    Returns (kept_auto, removed_auto, seed_skipped). Order of kept entries is
    preserved. Caller decides which tickers are stale enough to remove
    (typically: filter find_stale_watchlist() to entries with days != None).
    """
    auto = _load_auto_file()
    seed = set(config.WATCHLIST_SEED)
    remove = set(stale_tickers)
    kept = [t for t in auto if t not in remove]
    removed = [t for t in auto if t in remove]
    auto_set = set(auto)
    # Stale names that live only in the seed are protected, not removed.
    seed_skipped = [t for t in stale_tickers if t in seed and t not in auto_set]
    _write_auto_file(kept)
    return kept, removed, seed_skipped


# ── Pruning ─────────────────────────────────────────────────────

def _load_lastpass() -> dict:
    if os.path.exists(LASTPASS_PATH):
        try:
            with open(LASTPASS_PATH) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def _save_lastpass(lp: dict):
    with open(LASTPASS_PATH, "w") as f:
        json.dump(lp, f, indent=2, sort_keys=True)


def record_screener_pass(tickers: List[str]) -> None:
    """Stamp today's date on each ticker that just passed the CANSLIM screener.
    Called by run.py / rebalancer after a screener run."""
    lp = _load_lastpass()
    today = dt.date.today().isoformat()
    for t in tickers:
        lp[t] = today
    _save_lastpass(lp)


def find_stale_watchlist(stale_days: int = None) -> list:
    """Return [(ticker, days_since_last_pass), ...] for watchlist names that
    haven't cleared the CANSLIM screener in N days. Unknown = treated as stale."""
    threshold = stale_days or config.DISCOVERY_STALE_DAYS
    lp = _load_lastpass()
    today = dt.date.today()
    stale = []
    for t in config.WATCHLIST:
        last = lp.get(t)
        if last is None:
            stale.append((t, None))
            continue
        try:
            days = (today - dt.date.fromisoformat(last)).days
        except ValueError:
            stale.append((t, None))
            continue
        if days >= threshold:
            stale.append((t, days))
    return stale


# ── Audit log ───────────────────────────────────────────────────

def _log_run(df: pd.DataFrame, sources: dict, *, mode: str) -> None:
    """Append one JSON line per run to .cache/discovery.log."""
    record = {
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "candidates": len(sources),
        "valid": int(len(df)),
        "top_10": df.head(10)["ticker"].tolist() if not df.empty else [],
        "score_p10": float(df["composite_score"].quantile(0.10)) if not df.empty else None,
        "score_p50": float(df["composite_score"].quantile(0.50)) if not df.empty else None,
        "score_p90": float(df["composite_score"].quantile(0.90)) if not df.empty else None,
    }
    try:
        with open(DISCOVERY_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


# ── Display ─────────────────────────────────────────────────────

def display_results(df: pd.DataFrame, watchlist: dict):
    from tabulate import tabulate

    def fmt_pct(v):
        if v is None or (isinstance(v, float) and (np.isnan(v) or v != v)):
            return "N/A"
        return f"{v*100:+.1f}%"

    print(f"\n{'='*78}")
    print(f"  AUTO STOCK DISCOVERY — {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  {len(df)} ranked candidates")
    print(f"{'='*78}\n  ── TOP 20 ──")

    rows = []
    for _, r in df.head(20).iterrows():
        tags = []
        for cat in SCREEN_CRITERIA:
            if r.get(f"pass_{cat}"):
                tags.append(cat[0].upper())
        size = "S" if r["market_cap"] < 10e9 else "M" if r["market_cap"] < 50e9 else "L"
        rows.append([
            r["rank"],
            r["ticker"],
            r["name"][:18],
            f"${r['price']:.2f}" if r["price"] else "N/A",
            f"{r['market_cap_B']:.1f}B",
            size,
            f"{r['pe']:.1f}" if r["pe"] else "N/A",
            fmt_pct(r["roe"]),
            fmt_pct(r["rev_growth"]),
            fmt_pct(r["ret_3m"]),
            f"{r['rs_pct']:.0f}" if r.get("rs_pct") is not None else "N/A",
            f"{r['ipo_age_years']:.1f}y" if r.get("ipo_age_years") else "N/A",
            f"{r['composite_score']:.1f}",
            "".join(tags) or "-",
        ])
    print(tabulate(rows,
        headers=["#", "Tick", "Name", "Price", "MCap", "Sz", "P/E",
                 "ROE", "RevGr", "3M", "RS", "IPO", "Score", "Tags"],
        tablefmt="simple"))
    print("  Tags: G=Growth V=Value S=SMID-Mom Q=Quality-Div")

    for cat, meta in SCREEN_CRITERIA.items():
        tickers = watchlist.get(cat, [])
        if tickers:
            print(f"\n  ── {cat.upper()}: {meta['description']} ──")
            print(f"    {', '.join(tickers)}")

    current = set(config.WATCHLIST)
    new_finds = [t for t in df.head(30)["ticker"] if t not in current]
    if new_finds:
        print("\n  ── NEW DISCOVERIES (not in current watchlist) ──")
        for t in new_finds[:10]:
            row = df[df["ticker"] == t].iloc[0]
            srcs = row.get("sources", "")
            print(f"    {t:6s}  ${row['price']:>8.2f}  MCap:{row['market_cap_B']:.1f}B  "
                  f"RS:{row['rs_pct']:.0f}  RevGr:{fmt_pct(row['rev_growth'])}  "
                  f"Score:{row['composite_score']:.1f}  [{row['sector']}]  src={srcs}")


def display_prune(stale: list, *, confirmable_only: bool = False):
    if not stale:
        print(f"\n  ✓ No watchlist names stale beyond {config.DISCOVERY_STALE_DAYS} days.")
        return
    print(f"\n  ── STALE WATCHLIST NAMES (no CANSLIM pass in ≥ {config.DISCOVERY_STALE_DAYS} days) ──")
    if confirmable_only:
        print(f"  Re-run with --confirm to remove auto-discovered names from watchlist_auto.json.")
        print(f"  Hand-curated seed names (in config.py) are never removed.")
        print(f"  'never seen' entries are NEVER auto-pruned (could be newly added).\n")
    else:
        print(f"  Candidates for removal — use --prune --confirm to apply.\n")
    for t, days in stale:
        d = f"{days}d ago" if days is not None else "never seen (safe — not auto-pruned)"
        print(f"    {t:6s}  last passed: {d}")


# ── Main ────────────────────────────────────────────────────────

def main() -> int:
    args = sys.argv[1:]
    update_mode = "--update" in args
    trending_only = "--trending" in args
    include_reddit = "--include-reddit" in args
    prune_mode = "--prune" in args
    confirm = "--confirm" in args

    if prune_mode:
        stale = find_stale_watchlist()
        # Auto-prune only entries with explicit stale dates; "never seen" might
        # be newly added by --update / quant subagent and just haven't had a
        # chance to clear CANSLIM yet.
        removable = [t for t, days in stale if days is not None]
        display_prune(stale, confirmable_only=not confirm)
        if confirm:
            if not removable:
                print("\n  Nothing safe to auto-prune (all stale entries are 'never seen').")
                return 0
            kept, removed, seed_skipped = prune_stale_from_config(removable)
            if removed:
                print(f"\n  ✓ Removed {len(removed)} stale tickers from "
                      f"watchlist_auto.json: {', '.join(removed)}")
            else:
                print("\n  Nothing removed from watchlist_auto.json.")
            for t in seed_skipped:
                print(f"    • {t}: seed — left in place (hand-curated in config.py)")
            print(f"    watchlist_auto.json now has {len(kept)} auto tickers "
                  f"(config.py seed untouched).")
        return 0

    if trending_only:
        print("  Smart-money tickers (13F + ETF + ARK + Congress):")
        sm = get_smart_money_tickers()
        for t in sorted(sm.keys())[:40]:
            print(f"    {t:6s}  [{', '.join(sm[t])}]")
        if include_reddit:
            print("\n  Reddit trending:")
            for t in get_reddit_trending_tickers():
                print(f"    {t}")
        return 0

    df, sources = discover(include_reddit=include_reddit, verbose=True)
    if df.empty:
        print("  No results. Check network or rerun later.")
        return 1

    watchlist = generate_watchlist(df)
    display_results(df, watchlist)
    _log_run(df, sources, mode="update" if update_mode else "scan")

    if update_mode:
        new_tickers = df.head(50)["ticker"].tolist()
        combined = update_config_watchlist(new_tickers)
        print(f"\n  ✓ Appended top discoveries to watchlist_auto.json — "
              f"effective WATCHLIST is now {len(combined)} tickers "
              f"(config.py seed untouched).")
        print("    Run 'python3 run.py' to use the new watchlist.")
    else:
        print("\n  To auto-update watchlist:  python3 discovery.py --update")
        print("  To list stale names:        python3 discovery.py --prune")
    return 0


if __name__ == "__main__":
    sys.exit(main())
