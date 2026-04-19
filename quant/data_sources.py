"""External data-signal fetchers for the quant review subagent.

Five sources; each returns a normalized ExternalSignal. Fetchers never raise —
network errors, parsing failures, empty responses all yield a signal with
`error` populated and `data=[]`.

Built incrementally: Task 3 adds fetch_13f_filings; Tasks 4-7 add reddit /
etf-holdings / ark / congress; Task 8 adds fetch_all_externals as a parallel
orchestrator.
"""
from __future__ import annotations
import datetime as dt
import logging
import os
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from quant.schema import ExternalSignal


# ── 13F filings ─────────────────────────────────────────────────

# SEC CIKs for funds we track. Add/remove to tune the "smart money" roster.
_TRACKED_13F_FUNDS = {
    "0001067983": "Berkshire Hathaway",
    "0001336528": "Bridgewater",
    "0001167483": "Tiger Global Management",
    "0001037389": "Renaissance Technologies",
    "0001423053": "Citadel Advisors",
    "0001040273": "Third Point",
}

_SEC_BASE = "https://data.sec.gov"
# SEC requires a specific User-Agent with contact info.
# SEC requires a specific User-Agent with real contact info. Read from env
# so ops can set the actual address; default is the project's primary contact.
_SEC_USER_AGENT = os.environ.get(
    "SEC_CONTACT_EMAIL_UA",
    "stock-tracker research yyxworld@gmail.com",
)


def _sec_get(url: str, *, timeout: int = 20) -> bytes:
    """GET from SEC with required headers. Raises on non-200."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _SEC_USER_AGENT,
        "Accept": "application/json, text/html, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"SEC {url} returned {resp.status}")
        return resp.read()


def _fetch_latest_13f_for_cik(cik: str) -> Optional[dict]:
    """Fetch the most recent 13F-HR filing for a given CIK.

    Returns {"period_of_report": date, "top_20": [{"ticker", "value", "weight"}]}
    or None if no 13F found. Raises on unrecoverable errors.

    Info-table filenames are arbitrary (e.g. "50240.xml" — filer/preparer
    specific). We discover it by listing the accession directory's index.json
    and picking the XML that isn't `primary_doc.xml`.
    """
    import json as _json
    submissions_url = f"{_SEC_BASE}/submissions/CIK{cik.zfill(10)}.json"
    raw = _sec_get(submissions_url)
    submissions = _json.loads(raw)
    recent = submissions["filings"]["recent"]
    form_types = recent["form"]
    accession_numbers = recent["accessionNumber"]
    report_dates = recent["reportDate"]
    for idx, form in enumerate(form_types):
        if form != "13F-HR":
            continue
        accession = accession_numbers[idx].replace("-", "")
        base = f"{_SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession}"
        try:
            index_raw = _sec_get(f"{base}/index.json", timeout=20)
            index = _json.loads(index_raw)
        except Exception:
            continue
        items = index.get("directory", {}).get("item", []) or []
        # Find the info-table XML: .xml file, not primary_doc.xml, not
        # the submission's own index files.
        candidates = [
            f["name"] for f in items
            if f.get("name", "").lower().endswith(".xml")
            and f.get("name") != "primary_doc.xml"
        ]
        for fname in candidates:
            try:
                xml_bytes = _sec_get(f"{base}/{fname}", timeout=20)
                holdings = _parse_13f_info_table(xml_bytes)
                if not holdings:
                    continue
                total = sum(h["value"] for h in holdings)
                for h in holdings:
                    h["weight"] = h["value"] / total if total else 0.0
                holdings.sort(key=lambda h: h["value"], reverse=True)
                return {
                    "period_of_report": dt.date.fromisoformat(report_dates[idx]),
                    "top_20": holdings[:20],
                }
            except (urllib.error.HTTPError, ET.ParseError):
                continue
        # No info-table file parsed successfully — try next 13F
        return None
    return None


def _parse_13f_info_table(xml_bytes: bytes) -> list:
    """Parse a form13fInfoTable.xml into [{"ticker": name, "cusip": str, "value": int}]."""
    ns = {"n": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}
    tree = ET.fromstring(xml_bytes)
    rows = []
    for info in tree.findall("n:infoTable", ns):
        name = (info.findtext("n:nameOfIssuer", default="", namespaces=ns) or "").strip()
        cusip = (info.findtext("n:cusip", default="", namespaces=ns) or "").strip()
        value_raw = info.findtext("n:value", default="0", namespaces=ns)
        try:
            # 13F reports value in thousands of dollars
            value = int(float(value_raw)) * 1000
        except (TypeError, ValueError):
            value = 0
        rows.append({"ticker": name, "cusip": cusip, "value": value})
    return rows


def fetch_13f_filings() -> ExternalSignal:
    """Aggregate top holdings from tracked funds' most recent 13F-HR filings."""
    rows = []
    errors = []
    latest_date = None
    for cik, fund_name in _TRACKED_13F_FUNDS.items():
        try:
            result = _fetch_latest_13f_for_cik(cik)
        except Exception as e:
            errors.append(f"{fund_name}: {e}")
            continue
        if result is None:
            continue
        period = result["period_of_report"]
        latest_date = period if latest_date is None else max(latest_date, period)
        for holding in result["top_20"]:
            rows.append({
                "fund": fund_name,
                "ticker": holding.get("ticker", ""),
                "cusip": holding.get("cusip", ""),
                "value_usd": holding.get("value", 0),
                "weight": round(holding.get("weight", 0.0), 4),
            })
    if not rows:
        # All funds either errored or returned None — treat as error signal
        # so data_gaps captures it clearly.
        if errors:
            err_msg = "; ".join(errors[:3])
        else:
            err_msg = f"no 13F filings found (all {len(_TRACKED_13F_FUNDS)} tracked funds returned none)"
        return ExternalSignal(
            source="13F",
            as_of=dt.datetime.now(dt.timezone.utc),
            data=[],
            error=err_msg,
        )
    as_of = (dt.datetime.combine(latest_date, dt.time()).replace(tzinfo=dt.timezone.utc)
             if latest_date else dt.datetime.now(dt.timezone.utc))
    return ExternalSignal(source="13F", as_of=as_of, data=rows)


# ── Reddit trending ──────────────────────────────────────────────

import json as _json
import re


def _fetch_reddit_hot_posts(subreddits: tuple = ("wallstreetbets", "stocks", "investing"),
                            limit: int = 25) -> list:
    """Pull hot posts from each subreddit via Reddit's free JSON API."""
    posts = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "stock-tracker/1.0 (research)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise RuntimeError(f"reddit /r/{sub} returned {resp.status}")
            data = _json.loads(resp.read())
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            posts.append({
                "title": d.get("title", ""),
                "score": d.get("score", 0),
                "ts": d.get("created_utc", 0),
                "subreddit": sub,
            })
    return posts


_TICKER_RE = re.compile(r"\$?([A-Z]{2,5})\b")

# Words that look like tickers but aren't — filter false positives.
_TICKER_STOPWORDS = {
    "CEO", "CFO", "CTO", "IPO", "ETF", "GDP", "CPI", "FBI", "SEC", "FDA",
    "FED", "IMF", "NYSE", "AMEX", "OTC", "USA", "THE", "AND", "FOR", "BUT",
    "NOT", "YOU", "ALL", "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "HAS",
    "HIS", "HOW", "ITS", "MAY", "NEW", "NOW", "OLD", "ANY", "WHO", "DID",
    "GOT", "SAY", "SHE", "USE", "RUN", "BIG", "TOP", "LOW", "USD", "PM",
    "AM", "DD", "YOLO", "HODL", "FOMO", "ATH", "ATL", "DCA", "PE", "ROI",
    "EV", "AI", "US", "UK", "EU",
}


def _extract_tickers(title: str) -> set:
    """Pull candidate tickers from post title. Heuristic — false positives
    are filtered via the stopword set."""
    matches = _TICKER_RE.findall(title)
    return {m for m in matches if m not in _TICKER_STOPWORDS}


def fetch_reddit_trending() -> ExternalSignal:
    """Top tickers mentioned in hot posts across finance subreddits, with
    per-ticker mention count and sample titles."""
    try:
        posts = _fetch_reddit_hot_posts()
    except Exception as e:
        return ExternalSignal(
            source="reddit",
            as_of=dt.datetime.now(dt.timezone.utc),
            data=[],
            error=str(e),
        )
    counts: dict = {}
    for post in posts:
        title = post.get("title", "")
        for ticker in _extract_tickers(title):
            rec = counts.setdefault(ticker, {"count": 0, "titles": []})
            rec["count"] += 1
            if len(rec["titles"]) < 3:
                rec["titles"].append(title[:100])
    rows = [{"ticker": t, "mentions": rec["count"], "sample_titles": rec["titles"]}
            for t, rec in counts.items()]
    rows.sort(key=lambda r: r["mentions"], reverse=True)
    return ExternalSignal(
        source="reddit",
        as_of=dt.datetime.now(dt.timezone.utc),
        data=rows[:20],
    )


# ── Popular ETFs ────────────────────────────────────────────────

_TRACKED_ETFS = ["MAGS", "ARKK", "QQQ", "ICLN", "VGT"]


def _fetch_etf_top_holdings(symbol: str):
    """Return a DataFrame of top holdings for an ETF via yfinance.
    yfinance exposes holdings through Ticker(...).funds_data.top_holdings in
    recent versions; falls back to Ticker(...).info['holdings'] for older
    versions."""
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    funds_data = getattr(ticker, "funds_data", None)
    if funds_data is not None:
        th = getattr(funds_data, "top_holdings", None)
        if th is not None and not th.empty:
            return th.reset_index()
    info = ticker.info or {}
    holdings = info.get("holdings")
    if holdings:
        import pandas as pd
        return pd.DataFrame(holdings)
    raise RuntimeError(f"no holdings data for {symbol}")


def fetch_popular_etf_holdings() -> ExternalSignal:
    """Top ~25 holdings of each tracked thematic/broad ETF."""
    rows = []
    skipped = []
    for etf in _TRACKED_ETFS:
        try:
            df = _fetch_etf_top_holdings(etf)
        except Exception as e:
            skipped.append(f"{etf}: {e}")
            continue
        if df is None or df.empty:
            skipped.append(f"{etf}: empty")
            continue
        symbol_col = next((c for c in df.columns if c.lower() in ("symbol", "ticker")), None)
        weight_col = next((c for c in df.columns
                           if "percent" in c.lower() or c.lower() == "weight"), None)
        if symbol_col is None:
            skipped.append(f"{etf}: no symbol column")
            continue
        for _, row in df.head(25).iterrows():
            rows.append({
                "etf": etf,
                "ticker": str(row.get(symbol_col, "")).upper(),
                "weight": float(row.get(weight_col, 0.0)) if weight_col else 0.0,
            })
    error = "; ".join(skipped[:3]) if not rows and skipped else None
    return ExternalSignal(
        source="etf-holdings",
        as_of=dt.datetime.now(dt.timezone.utc),
        data=rows,
        error=error,
    )


# ── ARK / Cathie Wood ────────────────────────────────────────────

_ARK_CSV_URL = "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_Trades.csv"


def _fetch_ark_csv() -> str:
    """Download the live ARK trades CSV. Raises on non-200."""
    req = urllib.request.Request(_ARK_CSV_URL, headers={
        "User-Agent": "stock-tracker/1.0 (research)",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ARK CSV returned {resp.status}")
        return resp.read().decode("utf-8", errors="replace")


def fetch_ark_trades() -> ExternalSignal:
    """Last 7 days of ARK trades across their ETF family."""
    import csv
    import io
    try:
        blob = _fetch_ark_csv()
    except Exception as e:
        return ExternalSignal(source="ark", as_of=dt.datetime.now(dt.timezone.utc),
                              data=[], error=str(e))
    reader = csv.DictReader(io.StringIO(blob))
    cutoff = dt.date.today() - dt.timedelta(days=7)
    rows = []
    latest = None
    for raw in reader:
        date_str = (raw.get("date") or raw.get("Date") or "").strip()
        if not date_str:
            continue
        try:
            row_date = dt.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError:
            continue
        if row_date < cutoff:
            continue
        latest = row_date if latest is None else max(latest, row_date)
        direction = (raw.get("direction") or raw.get("Direction") or "").strip().lower()
        rows.append({
            "date": row_date.isoformat(),
            "fund": (raw.get("fund") or raw.get("Fund") or "").strip(),
            "direction": "buy" if "buy" in direction else ("sell" if "sell" in direction else direction),
            "ticker": (raw.get("ticker") or raw.get("Ticker") or "").strip().upper(),
            "shares": raw.get("shares") or raw.get("Shares") or "",
            "weight_pct": raw.get("weight(%)") or raw.get("Weight(%)") or "",
        })
    as_of = (dt.datetime.combine(latest, dt.time()).replace(tzinfo=dt.timezone.utc)
             if latest else dt.datetime.now(dt.timezone.utc))
    return ExternalSignal(source="ark", as_of=as_of, data=rows)


# ── Congress / Pelosi ────────────────────────────────────────────

_CAPITOLTRADES_API = "https://bff.capitoltrades.com/trades"


def _fetch_capitoltrades_json(days: int = 14) -> dict:
    """Fetch recent disclosed trades from capitoltrades' public endpoint.
    Sorted newest first; 100 items is plenty for a 14-day window."""
    url = f"{_CAPITOLTRADES_API}?page=1&pageSize=100"
    req = urllib.request.Request(url, headers={
        "User-Agent": "stock-tracker/1.0 (research)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"capitoltrades returned {resp.status}")
        return _json.loads(resp.read())


def fetch_congress_trades() -> ExternalSignal:
    """Disclosed congressional trades from the last ~14 days."""
    try:
        blob = _fetch_capitoltrades_json()
    except Exception as e:
        return ExternalSignal(source="congress",
                              as_of=dt.datetime.now(dt.timezone.utc),
                              data=[], error=str(e))
    cutoff = dt.date.today() - dt.timedelta(days=14)
    rows = []
    latest_disclosed = None
    for item in blob.get("data", []) or []:
        try:
            disclosed_date = dt.date.fromisoformat(item["disclosed"])
        except (KeyError, ValueError):
            continue
        if disclosed_date < cutoff:
            continue
        latest_disclosed = disclosed_date if latest_disclosed is None \
            else max(latest_disclosed, disclosed_date)
        politician = item.get("politician") or {}
        member = f"{politician.get('firstName', '')} {politician.get('lastName', '')}".strip()
        asset = item.get("asset") or {}
        rows.append({
            "member": member,
            "ticker": (asset.get("ticker") or "").upper(),
            "direction": (item.get("type") or "").lower(),
            "amount_range": item.get("value", ""),
            "trade_date": item.get("traded", ""),
            "disclosed_date": item.get("disclosed", ""),
        })
    as_of = (dt.datetime.combine(latest_disclosed, dt.time()).replace(tzinfo=dt.timezone.utc)
             if latest_disclosed else dt.datetime.now(dt.timezone.utc))
    return ExternalSignal(source="congress", as_of=as_of, data=rows)


# ── Orchestrator ─────────────────────────────────────────────────

from concurrent.futures import ThreadPoolExecutor, as_completed


def fetch_all_externals(timeout_per_source: int = 30) -> list:
    """Fetch all five external signals in parallel. Returns a list of
    ExternalSignal objects — always length 5, even if some failed.
    Each fetcher is also internally defensive (returns error-signals rather
    than raising), but this layer adds an outer catch-all."""
    fetchers = [
        ("13F", fetch_13f_filings),
        ("reddit", fetch_reddit_trending),
        ("etf-holdings", fetch_popular_etf_holdings),
        ("ark", fetch_ark_trades),
        ("congress", fetch_congress_trades),
    ]
    results: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): name for name, fn in fetchers}
        for future in as_completed(futures, timeout=timeout_per_source * 2):
            name = futures[future]
            try:
                results[name] = future.result(timeout=timeout_per_source)
            except Exception as e:
                results[name] = ExternalSignal(
                    source=name,
                    as_of=dt.datetime.now(dt.timezone.utc),
                    data=[],
                    error=str(e),
                )
    # Ensure all 5 are present even if some futures didn't complete
    for name, _ in fetchers:
        if name not in results:
            results[name] = ExternalSignal(
                source=name,
                as_of=dt.datetime.now(dt.timezone.utc),
                data=[],
                error="timed out",
            )
    return [results[name] for name, _ in fetchers]
