# Discovery Universe Expansion + Two-Stage Screening + Peer-Relative Ranking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace discovery's "S&P 500 only" scan with a broad, stable, rules-based universe (Russell 1000 via iShares full-holdings CSV — 方案A), make the scan affordable with a cheap-then-expensive two-stage pipeline (两段式), and fix the ranking so high-growth leaders aren't penalized by the value factor (peer-relative + growth-aware ranking).

**Architecture:** (1) A universe source (`get_universe_tickers`) parses iShares' official full-holdings CSV (≈1000 names incl. non-S&P leaders like MRVL), cached weekly, with the existing Wikipedia S&P 500 as fallback. (2) `discover()` becomes two-stage: Stage 1 batch-downloads OHLCV for the whole universe and ranks on universe-wide relative strength + a liquidity gate (cheap), keeping the top `DISCOVERY_STAGE1_KEEP`; Stage 2 fetches per-ticker info+fundamentals only for survivors (expensive) and computes the composite. (3) `compute_composite_scores` ranks the value/quality factors **within GICS sector** and neutralizes the value-P/E factor for the highest-growth cohort.

**Tech Stack:** Python 3.9, pandas/numpy, yfinance (via `data.py` cached helpers), `urllib`, pytest. No new third-party deps.

**Base branch:** `discovery-auto-watchlist` (HEAD `01560bb`). This plan builds on the already-merged full-S&P-500 scan and the `watchlist_auto.json` refactor.

**Key design decisions (why):**
- **Universe = iShares full-holdings CSV, not yfinance.** `quant/data_sources._fetch_etf_top_holdings` only returns the top ~25 holdings — useless for a full universe. iShares publishes a daily CSV of *all* holdings per ETF; that's the only free source for the complete Russell 1000 list.
- **Two-stage ordering.** Today `discover()` calls `fetch_snapshots_parallel` (one `fetch_info` + one `fetch_fundamentals` per ticker) on *every* candidate before any cheap price screen. Scanning a 1000-name universe that way = ~2000 per-ticker HTTP calls. Stage 1 cuts to ~250 survivors using one batched OHLCV download, so Stage 2 only pays per-ticker cost for survivors.
- **Universe-wide RS, survivor-relative fundamentals.** Relative strength is ranked across the *whole* universe in Stage 1 (stable, complete — the point-2 win). Fundamentals only exist for survivors (we never fetch them for the whole universe), so fundamental percentiles are necessarily survivor-relative; survivor membership is itself a stable function of the universe, so selection stays reproducible run-to-run.
- **Growth exemption is rank-based, not absolute-threshold.** An absolute rev-growth cutoff would break the existing `test_composite_score_is_rank_based` invariance (scaling rev_growth ×100 must not change ranks). A percentile cutoff is scale-invariant, so the existing property test stays valid.

---

## File Structure

- `config.py` — add the universe / two-stage / ranking constants (one new block near the existing `DISCOVERY_*` block, ~line 260).
- `discovery.py` — add `parse_ishares_holdings_csv`, `fetch_etf_full_holdings`, `get_universe_tickers`, `_chunked_ohlcv`, `prescreen_universe`, `_pct_rank_within`; modify `merge_candidates`, `discover`, `compute_composite_scores`.
- `tests/test_discovery.py` — new tests for the parser, universe source, merge-with-universe, two-stage `discover`, and the sector-relative / growth-exempt ranking. All offline (fixtures + monkeypatch).
- `tests/fixtures/ishares_iwb_sample.csv` — small CSV fixture mimicking the iShares layout (created in Task 2).
- `README.md` — update the Stock-discovery section + the data-sources table.

---

## Task 1: Config constants for universe, two-stage, and ranking

**Files:**
- Modify: `config.py` (after the existing `DISCOVERY_TICKER_SOURCES` block, ~line 266)
- Test: `tests/test_config_overrides.py` (append a test) — or `tests/test_discovery.py`; use `tests/test_discovery.py` to keep discovery config together.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_discovery.py`:

```python
# ── New-universe / two-stage / ranking config ─────────────────────

def test_discovery_universe_config_present_and_sane():
    import config
    assert isinstance(config.DISCOVERY_UNIVERSE_ETFS, dict) and config.DISCOVERY_UNIVERSE_ETFS
    # every value is a URL string
    assert all(isinstance(u, str) and u.startswith("http") for u in config.DISCOVERY_UNIVERSE_ETFS.values())
    assert config.DISCOVERY_UNIVERSE_MAX >= 1000
    assert 50 <= config.DISCOVERY_STAGE1_KEEP <= config.DISCOVERY_UNIVERSE_MAX
    assert config.DISCOVERY_MIN_PRICE > 0
    assert config.DISCOVERY_MIN_DOLLAR_VOLUME > 0
    assert isinstance(config.DISCOVERY_SECTOR_RELATIVE, bool)
    assert 0 <= config.DISCOVERY_GROWTH_EXEMPT_PCTL <= 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py::test_discovery_universe_config_present_and_sane -q`
Expected: FAIL with `AttributeError: module 'config' has no attribute 'DISCOVERY_UNIVERSE_ETFS'`

- [ ] **Step 3: Add the constants**

In `config.py`, immediately after the `DISCOVERY_TICKER_SOURCES = ( ... )` block (~line 266), insert:

```python
# ── Discovery scan universe (方案A: Russell 1000 via iShares full holdings) ──
# The union of these ETFs' FULL holdings is the discovery scan universe. iShares
# publishes a daily CSV of every holding (not just the top 25), giving ~1000
# large+mid-cap US names — far broader than the S&P 500, and it includes growth
# leaders that sit outside the index. Wikipedia S&P 500 is the fallback if the
# CSV download is blocked (see discovery.get_universe_tickers).
DISCOVERY_UNIVERSE_ETFS = {
    # iShares Russell 1000 ETF (IWB) — large + mid cap, rules-based, daily-updated.
    "IWB": (
        "https://www.ishares.com/us/products/239707/"
        "ishares-russell-1000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWB_holdings&dataType=fund"
    ),
}
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py::test_discovery_universe_config_present_and_sane -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_discovery.py
git commit -m "feat(discovery): config for Russell-1000 universe, two-stage, peer-relative ranking"
```

---

## Task 2: iShares holdings CSV parser

**Files:**
- Create: `tests/fixtures/ishares_iwb_sample.csv`
- Modify: `discovery.py` (add `parse_ishares_holdings_csv` after `get_sp500_tickers`, ~line 95)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/ishares_iwb_sample.csv` (mimics the real iShares layout: ~9 preamble lines, then a header row starting with `Ticker`, then holdings — including a cash/derivative row that must be filtered out):

```text
iShares Russell 1000 ETF
Fund Holdings as of,"Jun 02, 2026"
Inception Date,"May 15, 2000"
Shares Outstanding,"123,456,789"
Stock,"-"
Bonds,"-"
Cash,"-"
Other,"-"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,CUSIP,ISIN,SEDOL,Price,Location,Exchange,Currency
AAPL,APPLE INC,Information Technology,Equity,"1,000,000,000","6.50","1,000,000,000","5,000,000",037833100,US0378331005,2046251,200.00,United States,NASDAQ,USD
MRVL,MARVELL TECHNOLOGY INC,Information Technology,Equity,"50,000,000","0.33","50,000,000","170,000",577864105,US5778641050,B142Wc0,290.00,United States,NASDAQ,USD
BRK.B,BERKSHIRE HATHAWAY INC CLASS B,Financials,Equity,"40,000,000","0.27","40,000,000","100,000",084670702,US0846707026,2073390,400.00,United States,New York Stock Exchange Inc.,USD
USD,US DOLLAR,Cash and/or Derivatives,Cash,"5,000,000","0.03","5,000,000","5,000,000",-,-,-,1.00,United States,-,USD
MARGIN_USD,FUTURES MARGIN,Cash and/or Derivatives,Cash Collateral and Margins,"1,000,000","0.01","1,000,000","1,000,000",-,-,-,1.00,United States,-,USD
```

- [ ] **Step 2: Write the failing test**

Add to `tests/test_discovery.py`:

```python
import pathlib

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ishares_iwb_sample.csv"

def test_parse_ishares_holdings_csv_extracts_equities_only():
    text = _FIX.read_text()
    out = discovery.parse_ishares_holdings_csv(text)
    assert "AAPL" in out
    assert "MRVL" in out          # the whole point: a non-S&P leader is in Russell 1000
    assert "BRK.B" in out         # dotted share-class ticker kept
    assert "USD" not in out       # cash row filtered (Asset Class != Equity)
    assert "MARGIN_USD" not in out
    assert out == list(dict.fromkeys(out))  # de-duped, order preserved

def test_parse_ishares_holdings_csv_bad_input_returns_empty():
    assert discovery.parse_ishares_holdings_csv("") == []
    assert discovery.parse_ishares_holdings_csv("no header here\njust,junk\n") == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k ishares -q`
Expected: FAIL with `AttributeError: module 'discovery' has no attribute 'parse_ishares_holdings_csv'`

- [ ] **Step 4: Implement the parser**

In `discovery.py`, add near the top (after the existing `import re`, add `import csv` and `import io` to the imports), then insert after `get_sp500_tickers` (~line 95):

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py -k ishares -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add discovery.py tests/test_discovery.py tests/fixtures/ishares_iwb_sample.csv
git commit -m "feat(discovery): parse iShares full-holdings CSV into a ticker universe"
```

---

## Task 3: Universe source with weekly cache + S&P 500 fallback

**Files:**
- Modify: `discovery.py` (add `fetch_etf_full_holdings` + `get_universe_tickers` after `parse_ishares_holdings_csv`)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
def test_get_universe_tickers_unions_etfs_and_caches(monkeypatch, tmp_path):
    # No real network: stub the per-ETF fetch and the disk cache.
    monkeypatch.setattr(discovery, "CACHE_DIR", str(tmp_path))
    calls = {"n": 0}
    def fake_fetch(sym, url):
        calls["n"] += 1
        return ["AAPL", "MRVL", "NVDA"] + [f"X{i}" for i in range(300)]
    monkeypatch.setattr(discovery, "fetch_etf_full_holdings", fake_fetch)
    monkeypatch.setattr(config, "DISCOVERY_UNIVERSE_ETFS", {"IWB": "http://x"})
    u1 = discovery.get_universe_tickers()
    assert "MRVL" in u1 and "AAPL" in u1
    # second call served from cache → no extra fetch
    u2 = discovery.get_universe_tickers()
    assert u1 == u2
    assert calls["n"] == 1

def test_get_universe_tickers_falls_back_to_sp500(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(discovery, "fetch_etf_full_holdings", lambda s, u: [])  # download blocked
    monkeypatch.setattr(discovery, "get_sp500_tickers", lambda: ["SPX1", "SPX2", "SPX3"])
    monkeypatch.setattr(config, "DISCOVERY_UNIVERSE_ETFS", {"IWB": "http://x"})
    u = discovery.get_universe_tickers()
    assert u == ["SPX1", "SPX2", "SPX3"]
```

Note: `_cache_get`/`_cache_set` derive their path from `discovery.CACHE_DIR`; monkeypatching `CACHE_DIR` to `tmp_path` keeps the test off the real cache. Verify `_cache_get`/`_cache_set` read the module global `CACHE_DIR` (they do, line 58/66) — if they captured it at import you'd patch the path constant instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k universe_tickers -q`
Expected: FAIL with `AttributeError: ... has no attribute 'fetch_etf_full_holdings'`

- [ ] **Step 3: Implement**

In `discovery.py`, after `parse_ishares_holdings_csv`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py -k universe_tickers -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): get_universe_tickers (iShares union, weekly cache, S&P500 fallback)"
```

---

## Task 4: `merge_candidates` scans the full universe (replaces S&P 500 round-robin)

**Files:**
- Modify: `discovery.py` `merge_candidates` (lines 161-207) and its docstring
- Test: `tests/test_discovery.py` (update existing universe-coverage test name + add one)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_discovery.py`:

```python
def test_merge_candidates_uses_full_universe(monkeypatch):
    """merge_candidates now sources the bulk universe from get_universe_tickers,
    not the S&P 500 round-robin. Watchlist + smart-money still come first."""
    universe = [f"U{i}" for i in range(1200)]
    monkeypatch.setattr(discovery, "get_universe_tickers", lambda: universe)
    monkeypatch.setattr(discovery, "get_smart_money_tickers", lambda *a, **k: {"NVDA": ["13F"]})
    monkeypatch.setattr(config, "WATCHLIST", ["AAPL", "MSFT"])
    monkeypatch.setattr(config, "DISCOVERY_UNIVERSE_MAX", 2000)
    ordered, sources = discovery.merge_candidates()
    assert ordered[:2] == ["AAPL", "MSFT"]      # watchlist first
    assert "NVDA" in ordered                      # smart-money present
    assert set(universe).issubset(set(ordered))   # whole universe scanned
    assert "universe" in sources["U0"]            # tagged with its source
```

Also update the older `test_merge_candidates_includes_full_sp500` / `test_merge_candidates_priority_order` / `test_merge_candidates_respects_max_scan` / `test_merge_candidates_is_deterministic` / `test_merge_candidates_dedupes_across_sources`: replace any `monkeypatch.setattr(discovery, "sp500_round_robin_slice", ...)` with `monkeypatch.setattr(discovery, "get_universe_tickers", lambda: [...])`, and in priority-order assert the universe names land after smart-money/reddit. (The S&P 500 round-robin path is being removed from `merge_candidates`; the standalone `sp500_round_robin_slice`/`test_sp500_round_robin_*` tests stay — that function is still the fallback's building block via `get_sp500_tickers`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k merge_candidates -q`
Expected: FAIL (new test errors on `get_universe_tickers` not used by `merge_candidates` yet; some edited old tests fail too)

- [ ] **Step 3: Implement**

Replace the body of `merge_candidates` step 4 (lines 198-205) — the S&P 500 round-robin block — with the universe block, and update the docstring:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py -k "merge_candidates or sp500_round_robin" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): merge_candidates scans the full universe, not an S&P500 slice"
```

---

## Task 5: Stage-1 cheap pre-screen (`prescreen_universe`)

**Files:**
- Modify: `discovery.py` (add `_chunked_ohlcv` + `prescreen_universe` after `enrich_with_prices`, ~line 339)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
def _fake_ohlcv(tickers, days=300):
    idx = pd.date_range("2025-01-01", periods=days, freq="B")
    cols = {}
    for i, t in enumerate(tickers):
        # strictly rising series; steeper slope for lower i => higher RS for early tickers
        slope = 0.003 - 0.000002 * i
        series = 100 * np.cumprod(1 + np.full(days, max(slope, 0.0005)))
        cols[("Close", t)] = series
        cols[("Volume", t)] = pd.Series(2_000_000.0, index=idx)  # $ vol ~ price*2M >> floor
    df = pd.DataFrame(cols, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df

def test_prescreen_keeps_top_by_rs_plus_protected(monkeypatch):
    tickers = [f"T{i}" for i in range(100)]
    monkeypatch.setattr(discovery.data_mod, "fetch_ohlcv", lambda ts, period="1y": _fake_ohlcv(list(ts)))
    monkeypatch.setattr(config, "DISCOVERY_STAGE1_KEEP", 10)
    survivors, metrics = discovery.prescreen_universe(tickers, protected={"T99"})
    assert len(survivors) <= 10 + 1            # top-10 + the protected straggler
    assert "T0" in survivors                    # strongest RS kept
    assert "T99" in survivors                    # protected kept despite weak RS
    assert metrics["T0"]["rs_pct"] is not None   # metrics carried for reuse in stage 2

def test_prescreen_liquidity_gate_drops_illiquid(monkeypatch):
    tickers = ["LIQ", "ILLIQ"]
    def fake(ts, period="1y"):
        df = _fake_ohlcv(list(ts))
        df[("Volume", "ILLIQ")] = 1.0           # ~$ vol far below floor
        return df
    monkeypatch.setattr(discovery.data_mod, "fetch_ohlcv", fake)
    monkeypatch.setattr(config, "DISCOVERY_STAGE1_KEEP", 10)
    survivors, _ = discovery.prescreen_universe(tickers, protected=set())
    assert "LIQ" in survivors and "ILLIQ" not in survivors

def test_prescreen_failopen_when_no_prices(monkeypatch):
    tickers = [f"T{i}" for i in range(5)]
    monkeypatch.setattr(discovery.data_mod, "fetch_ohlcv", lambda ts, period="1y": pd.DataFrame())
    survivors, metrics = discovery.prescreen_universe(tickers, protected={"T0"})
    assert "T0" in survivors                     # never lose protected names
    assert metrics == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k prescreen -q`
Expected: FAIL with `AttributeError: ... has no attribute 'prescreen_universe'`

- [ ] **Step 3: Implement**

In `discovery.py`, after `enrich_with_prices` (~line 339):

```python
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
    dist_52w_high, sma50_dist_pct, price} so Stage 2 need not recompute prices.
    Fail-open: if no price data, carry protected + the raw head so a network blip
    never empties discovery.
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
        metrics[t] = {
            "price": last,
            "rs_pct": float(rs_pct.get(t)) if not pd.isna(rs_pct.get(t, np.nan)) else None,
            "ret_3m": float(ser.iloc[-1] / ser.iloc[-63] - 1) if len(ser) >= 63 else None,
            "dist_52w_high": float(last / ser.max() - 1) if ser.max() > 0 else None,
            "sma50_dist_pct": (
                float((last - ser.rolling(50).mean().iloc[-1]) / ser.rolling(50).mean().iloc[-1])
                if len(ser) >= 50 and ser.rolling(50).mean().iloc[-1] > 0 else None
            ),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py -k prescreen -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): Stage-1 prescreen_universe (universe-wide RS + liquidity gate)"
```

---

## Task 6: Wire two-stage into `discover()`

**Files:**
- Modify: `discovery.py` `discover` (lines 491-533)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
def test_discover_is_two_stage_only_fetches_snapshots_for_survivors(monkeypatch):
    candidates = [f"U{i}" for i in range(50)]
    monkeypatch.setattr(discovery, "merge_candidates",
                        lambda **k: (candidates, {t: ["universe"] for t in candidates}))
    # Stage 1 keeps a known small set.
    survivors = ["U0", "U1", "U2"]
    price_metrics = {t: {"rs_pct": 90.0, "ret_3m": 0.1, "dist_52w_high": -0.05,
                         "sma50_dist_pct": 0.05, "price": 100.0} for t in survivors}
    monkeypatch.setattr(discovery, "prescreen_universe", lambda c, protected, **k: (survivors, price_metrics))

    seen = {}
    def fake_snaps(ts, workers=None):
        seen["arg"] = list(ts)
        return [{"ticker": t, "name": t, "price": 100.0, "market_cap": 5e9,
                 "market_cap_B": 5.0, "pe": 20.0, "roe": 0.2, "rev_growth": 0.3,
                 "div_yield": 0.0, "sector": "Information Technology", "country": "United States",
                 "ipo_age_years": 4.0, "eps_q_growth": 0.2, "quarterly_eps": [], "debt_equity": 0.5,
                 "avg_volume": 1e6} for t in ts]
    monkeypatch.setattr(discovery, "fetch_snapshots_parallel", fake_snaps)

    df, _ = discovery.discover(verbose=False)
    assert seen["arg"] == survivors                      # ONLY survivors hit the expensive path
    assert set(df["ticker"]) == set(survivors)
    assert df.loc[df.ticker == "U0", "rs_pct"].iloc[0] == 90.0   # stage-1 metrics carried through
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k two_stage -q`
Expected: FAIL (current `discover` fetches snapshots for all candidates and calls `enrich_with_prices`, so `seen["arg"]` != survivors)

- [ ] **Step 3: Implement**

Replace `discover` (lines 491-533) with:

```python
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
```

(`enrich_with_prices` stays in the module — still unit-tested — but is no longer called by `discover`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_discovery.py -k two_stage -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): two-stage discover (prescreen → snapshots for survivors only)"
```

---

## Task 7: Peer-relative + growth-aware composite ranking

**Files:**
- Modify: `discovery.py` (add `_pct_rank_within` near `_pct_rank` ~line 367; modify `compute_composite_scores` lines 370-402)
- Test: `tests/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
def test_value_pe_ranked_within_sector(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_SECTOR_RELATIVE", True)
    monkeypatch.setattr(config, "DISCOVERY_GROWTH_EXEMPT_PCTL", 101.0)  # disable exemption
    # Two sectors. In Tech, A has lower PE than B → A ranks higher *within Tech*,
    # even though globally A's PE (30) is higher than Util C's PE (10).
    df = pd.DataFrame([
        {"ticker": "A", "sector": "Tech", "pe": 30, "rev_growth": 0.05, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
        {"ticker": "B", "sector": "Tech", "pe": 60, "rev_growth": 0.05, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
        {"ticker": "C", "sector": "Util", "pe": 10, "rev_growth": 0.05, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
    ])
    scored = discovery.compute_composite_scores(df.copy())
    a = scored.loc[scored.ticker == "A", "rank_value_pe"].iloc[0]
    b = scored.loc[scored.ticker == "B", "rank_value_pe"].iloc[0]
    assert a > b   # cheaper-within-sector ranks higher

def test_growth_exemption_neutralizes_value_pe(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_SECTOR_RELATIVE", False)
    monkeypatch.setattr(config, "DISCOVERY_GROWTH_EXEMPT_PCTL", 66.0)
    df = pd.DataFrame([
        {"ticker": "HIGROW", "sector": "Tech", "pe": 100, "rev_growth": 0.90, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
        {"ticker": "LOGROW1", "sector": "Tech", "pe": 12, "rev_growth": 0.02, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
        {"ticker": "LOGROW2", "sector": "Tech", "pe": 20, "rev_growth": 0.03, "roe": 0.1,
         "rs_pct": 50, "eps_q_growth": 0.0, "ret_3m": 0.0, "dist_52w_high": -0.1,
         "ipo_age_years": 5, "sma50_dist_pct": 0.0, "quarterly_eps": []},
    ])
    scored = discovery.compute_composite_scores(df.copy())
    # HIGROW has the worst PE but is in the top growth cohort → value_pe neutralized to 50,
    # NOT the lowest rank.
    assert scored.loc[scored.ticker == "HIGROW", "rank_value_pe"].iloc[0] == 50.0
```

Confirm the existing `test_composite_score_is_rank_based`, `test_composite_score_excludes_negative_pe_from_value`, and `test_composite_score_uses_config_weights` STILL PASS unchanged (no `sector` col → global fallback; rank-based exemption is scale-invariant).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_discovery.py -k "value_pe or growth_exemption" -q`
Expected: FAIL (`rank_value_pe` is currently global; exemption not implemented)

- [ ] **Step 3: Implement**

In `discovery.py`, add after `_pct_rank` (~line 367):

```python
def _pct_rank_within(series: pd.Series, groups: Optional[pd.Series], ascending: bool = True) -> pd.Series:
    """Percentile rank 0-100 computed WITHIN each group (e.g. GICS sector).

    Falls back to a global rank when groups is None/empty or a group has a single
    member (a singleton can't be ranked against peers). NaN → 50 (neutral)."""
    if groups is None or groups.isna().all():
        return _pct_rank(series, ascending=ascending)
    out = pd.Series(np.nan, index=series.index)
    for g, idx in groups.groupby(groups).groups.items():
        sub = series.loc[idx]
        if len(sub) <= 1:
            out.loc[idx] = _pct_rank(series, ascending=ascending).loc[idx]  # global for singletons
        else:
            out.loc[idx] = _pct_rank(sub, ascending=ascending)
    return out.fillna(50.0)
```

Then modify `compute_composite_scores` — replace the `value_pe` / `roe` lines (currently lines 381 and 388-391) so they use sector-relative ranking and the growth exemption:

```python
def compute_composite_scores(df: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
    """Cross-sectional rank for each dimension, weighted sum to `composite_score`.

    Momentum/RS factors rank market-wide; value/quality (value_pe, roe) rank
    within GICS sector when config.DISCOVERY_SECTOR_RELATIVE and a 'sector' column
    is present. The top growth cohort (rev_growth percentile >=
    config.DISCOVERY_GROWTH_EXEMPT_PCTL) is not penalized on value_pe.
    """
    if df.empty:
        df["composite_score"] = pd.Series(dtype=float)
        return df
    w = weights or config.DISCOVERY_WEIGHTS

    sector = df.get("sector") if (config.DISCOVERY_SECTOR_RELATIVE and "sector" in df.columns) else None
    if sector is not None:
        sector = sector.replace("", np.nan)

    pct = pd.DataFrame(index=df.index)
    pct["rs"]            = _pct_rank(df.get("rs_pct"))
    pct["rev_growth"]    = _pct_rank(df.get("rev_growth"))
    pct["eps_q_growth"]  = _pct_rank(df.get("eps_q_growth"))
    pct["roe"]           = _pct_rank_within(df.get("roe"), sector)
    pct["mom_3m"]        = _pct_rank(df.get("ret_3m"))
    pct["dist_52w_high"] = _pct_rank(df.get("dist_52w_high"))
    pct["ipo_age"]       = _pct_rank(df.get("ipo_age_years"), ascending=False)
    pct["sma50_dist"]    = _pct_rank(df.get("sma50_dist_pct"))

    pe = df.get("pe")
    inv_pe = pd.Series(np.where((pe > 0) & pe.notna(), 1.0 / pe, np.nan), index=df.index)
    value_pe = _pct_rank_within(inv_pe, sector)
    # Growth exemption (rank-based → scale-invariant): neutralize value_pe for the
    # top growth cohort so high-P/E leaders aren't dinged for being expensive.
    rev_pctl = _pct_rank(df.get("rev_growth"))
    value_pe = value_pe.mask(rev_pctl >= config.DISCOVERY_GROWTH_EXEMPT_PCTL, 50.0)
    pct["value_pe"] = value_pe

    eps_accel = df.get("quarterly_eps").apply(_eps_acceleration_score)
    total_w = sum(w.values())
    score = sum(pct[k] * w[k] for k in w if k in pct.columns) / max(total_w, 1e-9)
    df["composite_score"] = (score + 5.0 * eps_accel).round(2)
    df["eps_accel_score"] = eps_accel
    for k in pct.columns:
        df[f"rank_{k}"] = pct[k].round(1)
    return df
```

- [ ] **Step 4: Run the full discovery + config suite to verify pass**

Run: `python3 -m pytest tests/test_discovery.py tests/test_config_cleanup.py tests/test_config_overrides.py -q`
Expected: PASS (all green, including the unchanged composite-score property tests)

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): sector-relative value/quality ranking + growth-exempt value_pe"
```

---

## Task 8: Docs — README + module docstring

**Files:**
- Modify: `README.md` (Stock-discovery section ~line 100-110; data-sources table ~line 353)
- Modify: `discovery.py` module docstring (lines 2-30)

- [ ] **Step 1: Update README**

Replace the "Stock discovery" comment block (the lines added earlier about S&P-500-only) with:

```text
# Stock discovery
# Two-stage scan over a broad, stable universe:
#   Universe = config.DISCOVERY_UNIVERSE_ETFS holdings (Russell 1000 via iShares
#     IWB full-holdings CSV, ~1000 large+mid-cap US names; Wikipedia S&P 500 is
#     the fallback). Includes growth leaders outside the S&P 500 (e.g. MRVL).
#   Stage 1 (cheap): batch OHLCV → universe-wide relative strength + liquidity
#     gate → keep top DISCOVERY_STAGE1_KEEP survivors (watchlist/smart-money are
#     'protected' and always advance).
#   Stage 2 (expensive): info + fundamentals only for survivors → composite rank.
#   Ranking: momentum/RS market-wide; value/quality within GICS sector; the top
#     growth cohort is exempt from the value-P/E penalty.
python3 discovery.py                # scan market for new candidates
```

Also update the data-sources table row (~line 353): change `| Wikipedia | Free | S&P 500 component list |` to add a row:
```text
| iShares (IWB) | Free | Russell 1000 full holdings — discovery scan universe |
```

- [ ] **Step 2: Update the module docstring**

In `discovery.py`, replace the "Pipeline:" section (lines 6-13) so it describes the universe + two stages (universe source, Stage 1 prescreen, Stage 2 snapshots, peer-relative ranking). Keep the `watchlist_auto.json` paragraph (lines 18-21) intact.

- [ ] **Step 3: Verify docs reference real symbols**

Run: `grep -n "DISCOVERY_STAGE1_KEEP\|DISCOVERY_UNIVERSE_ETFS\|prescreen" README.md discovery.py`
Expected: matches in both files (no stale `round-robin` / `DISCOVERY_SP500_BATCH` claims left in the discovery prose).

- [ ] **Step 4: Commit**

```bash
git add README.md discovery.py
git commit -m "docs(discovery): document Russell-1000 universe, two-stage scan, peer-relative ranking"
```

---

## Task 9: Manual live verification (network — run once, not a unit test)

**Files:** none (operational check)

- [ ] **Step 1: Confirm the universe loads and includes MRVL**

Run:
```bash
python3 -c "import discovery; u=discovery.get_universe_tickers(); print(len(u), 'MRVL' in u)"
```
Expected: a count ≥ ~950 and `True` (MRVL now in the universe). If it prints a small number, the iShares CSV download was blocked and it fell back to S&P 500 — check the URL / User-Agent.

- [ ] **Step 2: Dry scan (no writes)**

Run: `python3 discovery.py 2>&1 | tail -40`
Expected: candidate count ≈ universe size; "Stage 1 ... survivors → Stage 2"; a TOP-20 table; MRVL (and other non-S&P leaders) eligible to appear. Confirm Stage 2 fetched far fewer than the full universe (the printed survivor count ≈ `DISCOVERY_STAGE1_KEEP`).

- [ ] **Step 3: Record the outcome** in the PR description (universe size, survivor count, whether MRVL surfaced, wall-clock). Do NOT run `--update` as part of verification unless you intend to write `watchlist_auto.json`.

---

## Self-Review

**Spec coverage:**
- 方案A (broad universe) → Tasks 2-4 (iShares parser, `get_universe_tickers`, `merge_candidates` rewrite). ✅
- 两段式 (two-stage) → Tasks 5-6 (`prescreen_universe`, `discover` rewrite). ✅
- 排名设计 (point 2 stability + point 3 value-factor fix) → Task 7 (universe-wide RS via Stage 1 + sector-relative value/quality + rank-based growth exemption). ✅
- Config knobs → Task 1. Docs → Task 8. Live check → Task 9. ✅

**Backward-compat checks:**
- `sp500_round_robin_slice` + its tests retained (still the fallback's building block). ✅
- `enrich_with_prices` retained + still unit-tested, just no longer called by `discover`. ✅
- Existing composite-score property tests pass unchanged: no `sector` col → `_pct_rank_within` global fallback; growth exemption rank-based → scale-invariant. ✅

**Type/name consistency:** `prescreen_universe` returns `(survivors: list, metrics: dict)` — consumed exactly that way in `discover` (Task 6). `_pct_rank_within(series, groups, ascending)` signature matches both call sites in Task 7. `get_universe_tickers()` used by `merge_candidates` (Task 4) and Task 9. `DISCOVERY_GROWTH_EXEMPT_PCTL` (percentile, not absolute) used identically in config (Task 1) and ranking (Task 7).

**Placeholder scan:** none — every code/test step contains full content.

**Cost note (no silent caps):** `DISCOVERY_UNIVERSE_MAX=2000` and `DISCOVERY_STAGE1_KEEP=250` are explicit ceilings; `prescreen_universe` logs survivor count via `discover`'s verbose print so truncation is visible, not silent.
```
