#!/usr/bin/env python3
"""
Auto Stock Discovery — Refreshes the watchlist by scanning the market.

Instead of a static watchlist, this module discovers new candidates by:
  1. Scanning S&P 500, S&P 400 MidCap, Russell 2000 components
  2. Scraping Yahoo Finance screener results (gainers, most active, trending)
  3. Pulling Reddit/news trending tickers
  4. Filtering through our quality/value/momentum criteria
  5. Outputting a fresh ranked watchlist

Usage:
  python3 discovery.py                # full scan
  python3 discovery.py --trending     # just trending/momentum
  python3 discovery.py --update       # scan + update config.py watchlist
"""
import sys
import os
import json
import time
import re
from datetime import datetime
from typing import List, Dict

import pandas as pd
import numpy as np
import yfinance as yf

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_get(key: str, ttl_hours: float = 12):
    path = os.path.join(CACHE_DIR, f"disc_{key}.json")
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl_hours * 3600:
            with open(path) as f:
                return json.load(f)
    return None


def _cache_set(key: str, data):
    path = os.path.join(CACHE_DIR, f"disc_{key}.json")
    with open(path, "w") as f:
        json.dump(data, f, default=str)


# ── Source 1: Index Components ──────────────────────────────────

def get_sp500_tickers() -> List[str]:
    """Get S&P 500 tickers from Wikipedia."""
    cached = _cache_get("sp500", ttl_hours=168)  # cache 1 week
    if cached:
        return cached

    try:
        import urllib.request
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode()

        # Parse tickers from the table
        tickers = re.findall(r'<a[^>]*class="external text"[^>]*>([A-Z]{1,5})</a>', html)
        if not tickers:
            # Fallback pattern
            tickers = re.findall(r'ticker[^>]*>([A-Z]{1,5})<', html)
        if not tickers:
            # Another fallback: find NYSE/NASDAQ stock symbols in table rows
            tickers = re.findall(r'<td[^>]*><a[^>]*>([A-Z]{1,5})</a>', html)

        # Clean up — only keep likely tickers
        tickers = [t for t in tickers if len(t) >= 1 and t.isalpha()]
        tickers = list(dict.fromkeys(tickers))  # deduplicate, keep order

        if len(tickers) > 100:
            _cache_set("sp500", tickers)
            return tickers
    except Exception:
        pass

    # Hardcoded fallback of major S&P 500 names
    return []


def get_yahoo_screener(screen_type: str = "most_actives") -> List[str]:
    """Get tickers from Yahoo Finance screener categories.

    screen_type: most_actives, day_gainers, day_losers, trending
    """
    cached = _cache_get(f"yahoo_{screen_type}", ttl_hours=4)
    if cached:
        return cached

    try:
        import urllib.request
        url = f"https://finance.yahoo.com/screener/predefined/{screen_type}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode()

        # Extract tickers from Yahoo's page
        tickers = re.findall(r'data-symbol="([A-Z]{1,5})"', html)
        if not tickers:
            tickers = re.findall(r'"/quote/([A-Z]{1,5})"', html)

        tickers = list(dict.fromkeys(tickers))[:50]
        if tickers:
            _cache_set(f"yahoo_{screen_type}", tickers)
        return tickers
    except Exception:
        return []


def get_trending_from_reddit() -> List[str]:
    """Extract trending ticker mentions from Reddit finance subs."""
    try:
        from sentiment import fetch_all_reddit, _extract_tickers, NOT_TICKERS
        from collections import Counter

        posts = fetch_all_reddit()
        ticker_counts = Counter()
        for p in posts:
            text = f"{p.get('title', '')} {p.get('selftext', '')}"
            tickers = _extract_tickers(text)
            engagement = p.get("score", 0) + p.get("num_comments", 0)
            for t in tickers:
                ticker_counts[t] += engagement

        # Return top tickers by weighted mentions
        return [t for t, _ in ticker_counts.most_common(30)]
    except Exception:
        return []


# ── Fundamental Screening ───────────────────────────────────────

# Criteria for different tiers
SCREEN_CRITERIA = {
    "growth": {
        "description": "High revenue growth, momentum names",
        "min_rev_growth": 0.15,
        "max_pe": None,          # growth stocks can have high P/E
        "min_market_cap": 1e9,
        "max_market_cap": 100e9,
    },
    "value": {
        "description": "Undervalued with decent quality",
        "min_rev_growth": None,
        "max_pe": 25,
        "min_roe": 0.10,
        "min_market_cap": 2e9,
        "max_market_cap": None,
    },
    "smid_momentum": {
        "description": "Small/mid-cap with strong price momentum",
        "min_rev_growth": 0.10,
        "max_pe": None,
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


def screen_ticker(ticker: str) -> dict:
    """Fetch and compute screening metrics for a single ticker."""
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return None

    if not info or info.get("quoteType") != "EQUITY":
        return None

    mcap = info.get("marketCap", 0) or 0
    if mcap < 300e6:  # skip micro-caps
        return None

    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = info.get("returnOnEquity")
    de = info.get("debtToEquity")
    if de is not None:
        de = de / 100
    rev_growth = info.get("revenueGrowth", 0) or 0
    div_yield = info.get("dividendYield", 0) or 0
    price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
    avg_vol = info.get("averageVolume", 0) or 0
    beta = info.get("beta", 1) or 1
    sector = info.get("sector", "")
    name = info.get("shortName", ticker)

    # Price momentum (3-month)
    ret_3m = None
    above_sma50 = False
    try:
        hist = yf.download(ticker, period="6mo", progress=False)
        if not hist.empty:
            if isinstance(hist.columns, pd.MultiIndex):
                close = hist["Close"][ticker]
            else:
                close = hist["Close"]
            close = close.dropna()
            if len(close) >= 63:
                ret_3m = close.iloc[-1] / close.iloc[-63] - 1
            if len(close) >= 50:
                sma50 = close.rolling(50).mean().iloc[-1]
                above_sma50 = close.iloc[-1] > sma50
    except Exception:
        pass

    return {
        "ticker": ticker,
        "name": name[:25],
        "price": price,
        "market_cap": mcap,
        "market_cap_B": mcap / 1e9,
        "pe": pe,
        "roe": roe,
        "debt_equity": de,
        "rev_growth": rev_growth,
        "div_yield": div_yield,
        "avg_volume": avg_vol,
        "beta": beta,
        "sector": sector,
        "ret_3m": ret_3m,
        "above_sma50": above_sma50,
    }


def passes_criteria(stock: dict, criteria: dict) -> bool:
    """Check if a stock passes the given screening criteria."""
    if criteria.get("min_market_cap") and stock["market_cap"] < criteria["min_market_cap"]:
        return False
    if criteria.get("max_market_cap") and stock["market_cap"] > criteria["max_market_cap"]:
        return False
    if criteria.get("max_pe") and stock["pe"] and stock["pe"] > criteria["max_pe"]:
        return False
    if criteria.get("min_roe") and stock["roe"] and stock["roe"] < criteria["min_roe"]:
        return False
    if criteria.get("min_rev_growth") and stock["rev_growth"] < criteria["min_rev_growth"]:
        return False
    if criteria.get("min_div_yield") and stock["div_yield"] < criteria["min_div_yield"]:
        return False
    if criteria.get("max_debt_equity") and stock["debt_equity"] and stock["debt_equity"] > criteria["max_debt_equity"]:
        return False
    return True


def composite_score(stock: dict) -> float:
    """Compute composite ranking score for a stock."""
    score = 0

    # Revenue growth (0-30 pts)
    rg = stock.get("rev_growth", 0) or 0
    score += min(30, rg * 100)

    # ROE (0-20 pts)
    roe = stock.get("roe", 0) or 0
    score += min(20, roe * 50)

    # Momentum — 3-month return (0-20 pts)
    ret = stock.get("ret_3m", 0) or 0
    score += max(-10, min(20, ret * 40))

    # Value — inverse P/E (0-15 pts)
    pe = stock.get("pe")
    if pe and pe > 0:
        score += min(15, 300 / pe)

    # Above 50-SMA bonus (0-10 pts)
    if stock.get("above_sma50"):
        score += 10

    # Dividend bonus (0-5 pts)
    dy = stock.get("div_yield", 0) or 0
    score += min(5, dy * 100)

    return round(score, 2)


# ── Main Discovery Pipeline ────────────────────────────────────

def discover(max_scan: int = 100, verbose: bool = True) -> pd.DataFrame:
    """Run the full discovery pipeline.

    1. Gather candidate tickers from multiple sources
    2. Screen each through fundamentals
    3. Score and rank
    4. Return top candidates
    """
    if verbose:
        print("  Gathering candidate tickers...")

    # Collect candidates from all sources
    candidates = set()

    # Yahoo screeners
    for screen in ["most_actives", "day_gainers"]:
        tickers = get_yahoo_screener(screen)
        candidates.update(tickers)
        if verbose:
            print(f"    Yahoo {screen}: {len(tickers)} tickers")

    # Reddit trending
    reddit_tickers = get_trending_from_reddit()
    candidates.update(reddit_tickers)
    if verbose:
        print(f"    Reddit trending: {len(reddit_tickers)} tickers")

    # S&P 500 (sample — don't screen all 500)
    sp500 = get_sp500_tickers()
    if sp500:
        import random
        sample = random.sample(sp500, min(30, len(sp500)))
        candidates.update(sample)
        if verbose:
            print(f"    S&P 500 sample: {len(sample)} tickers")

    # Current watchlist (always include)
    from config import WATCHLIST
    candidates.update(WATCHLIST)
    if verbose:
        print(f"    Current watchlist: {len(WATCHLIST)} tickers")

    # Limit total scan
    candidates = list(candidates)[:max_scan]
    if verbose:
        print(f"    Total unique candidates: {len(candidates)}")
        print(f"\n  Screening fundamentals (this takes a minute)...")

    # Screen each ticker
    results = []
    cached_key = f"discovery_{datetime.now().strftime('%Y%m%d')}"
    cached = _cache_get(cached_key, ttl_hours=8)
    if cached:
        results = cached
        if verbose:
            print(f"    Using cached results ({len(results)} stocks)")
    else:
        for i, t in enumerate(candidates):
            if verbose and i % 20 == 0 and i > 0:
                print(f"    ...scanned {i}/{len(candidates)}")
            data = screen_ticker(t)
            if data:
                data["composite_score"] = composite_score(data)
                results.append(data)

        _cache_set(cached_key, results)
        if verbose:
            print(f"    Screened {len(results)} valid stocks out of {len(candidates)}")

    df = pd.DataFrame(results)
    if df.empty:
        return df

    df = df.sort_values("composite_score", ascending=False)
    df["rank"] = range(1, len(df) + 1)

    # Tag which criteria each stock passes
    for crit_name, crit in SCREEN_CRITERIA.items():
        df[f"pass_{crit_name}"] = df.apply(
            lambda row: passes_criteria(row.to_dict(), crit), axis=1)

    return df


def generate_watchlist(df: pd.DataFrame, max_per_category: int = 8) -> dict:
    """Generate a categorized watchlist from discovery results."""
    watchlist = {
        "growth": [],
        "value": [],
        "smid_momentum": [],
        "quality_dividend": [],
    }

    for cat in watchlist:
        col = f"pass_{cat}"
        if col in df.columns:
            passed = df[df[col]].head(max_per_category)
            watchlist[cat] = passed["ticker"].tolist()

    # Also pick overall top regardless of category
    watchlist["top_overall"] = df.head(15)["ticker"].tolist()

    return watchlist


def update_config_watchlist(new_tickers: List[str]):
    """Update the WATCHLIST in config.py with discovered tickers."""
    config_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(config_path) as f:
        content = f.read()

    # Keep existing tickers and add new ones
    from config import WATCHLIST
    combined = list(dict.fromkeys(WATCHLIST + new_tickers))  # deduplicate, keep order

    # Format the new watchlist
    lines = []
    for i, t in enumerate(combined):
        lines.append(f'    "{t}",')

    new_block = "WATCHLIST = [\n" + "\n".join(lines) + "\n]"

    # Replace the WATCHLIST block in config.py
    pattern = r'WATCHLIST\s*=\s*\[.*?\]'
    new_content = re.sub(pattern, new_block, content, flags=re.DOTALL)

    with open(config_path, "w") as f:
        f.write(new_content)

    return combined


# ── Display ─────────────────────────────────────────────────────

def display_results(df: pd.DataFrame, watchlist: dict):
    from tabulate import tabulate

    def fmt_pct(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "N/A"
        return f"{v*100:+.1f}%"

    print(f"\n{'='*70}")
    print(f"  AUTO STOCK DISCOVERY — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  Scanned {len(df)} stocks | Ranked by composite score")
    print(f"{'='*70}")

    # Top 20 overall
    print(f"\n  ── TOP 20 DISCOVERIES ──")
    rows = []
    for _, r in df.head(20).iterrows():
        tags = []
        for cat in SCREEN_CRITERIA:
            if r.get(f"pass_{cat}"):
                tags.append(cat[0].upper())
        tag_str = "".join(tags) if tags else "-"

        size = "S" if r["market_cap"] < 10e9 else "M" if r["market_cap"] < 50e9 else "L"

        rows.append([
            r["rank"],
            r["ticker"],
            r["name"][:20],
            f"${r['price']:.2f}" if r["price"] else "N/A",
            f"{r['market_cap_B']:.1f}B",
            size,
            f"{r['pe']:.1f}" if r["pe"] else "N/A",
            fmt_pct(r["roe"]),
            fmt_pct(r["rev_growth"]),
            fmt_pct(r["ret_3m"]),
            "Y" if r["above_sma50"] else "N",
            f"{r['composite_score']:.1f}",
            tag_str,
        ])

    print(tabulate(rows,
        headers=["#", "Tick", "Name", "Price", "MCap", "Sz", "P/E",
                 "ROE", "RevGr", "3M", ">50d", "Score", "Tags"],
        tablefmt="simple"))
    print("  Tags: G=Growth V=Value S=SMID-Momentum Q=Quality-Dividend")

    # Category breakdowns
    for cat, desc in [(k, v["description"]) for k, v in SCREEN_CRITERIA.items()]:
        tickers = watchlist.get(cat, [])
        if tickers:
            print(f"\n  ── {cat.upper()}: {desc} ──")
            print(f"    {', '.join(tickers)}")

    # Suggested new additions (not in current watchlist)
    from config import WATCHLIST
    current = set(WATCHLIST)
    new_finds = [t for t in df.head(30)["ticker"] if t not in current]
    if new_finds:
        print(f"\n  ── NEW DISCOVERIES (not in current watchlist) ──")
        for t in new_finds[:10]:
            row = df[df["ticker"] == t].iloc[0]
            print(f"    {t:6s}  ${row['price']:>8.2f}  MCap:{row['market_cap_B']:.1f}B  "
                  f"RevGr:{fmt_pct(row['rev_growth'])}  3M:{fmt_pct(row['ret_3m'])}  "
                  f"Score:{row['composite_score']:.1f}  [{row['sector']}]")


# ── Main ────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    update_mode = "--update" in args
    trending_only = "--trending" in args

    if trending_only:
        print("  Fetching trending tickers from Reddit + Yahoo...")
        reddit = get_trending_from_reddit()
        yahoo = get_yahoo_screener("most_actives") + get_yahoo_screener("day_gainers")
        all_trending = list(dict.fromkeys(reddit + yahoo))
        print(f"\n  Trending tickers ({len(all_trending)}):")
        for t in all_trending[:30]:
            print(f"    {t}")
        return

    df = discover(max_scan=100, verbose=True)
    if df.empty:
        print("  No results. Try again later.")
        return

    watchlist = generate_watchlist(df)
    display_results(df, watchlist)

    if update_mode:
        # Add top discoveries to watchlist
        new_tickers = df.head(20)["ticker"].tolist()
        combined = update_config_watchlist(new_tickers)
        print(f"\n  ✓ Updated config.py WATCHLIST: {len(combined)} tickers")
        print(f"    Run 'python3 run.py' to use the new watchlist.")
    else:
        print(f"\n  To auto-update the watchlist, run:")
        print(f"    python3 discovery.py --update")


if __name__ == "__main__":
    main()
