# Two-track Russell 3000 Value Screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `value` ensemble strategy with a staged, two-track (profitable / unprofitable-growth) screen over the Russell 3000, decomposed into focused modules.

**Architecture:** `discovery` sources the universe (IWV CSV); `value_prefilter` cuts ~3000 → a few hundred on price/volume only; `value_fundamentals` normalizes yfinance `.info` into a typed `Fundamentals`; `value_tracks` holds the pure two-track rules + scoring; `value_screen` is a thin orchestrator that wires the stages and writes the unchanged `{ticker,score,rank,factors}` strategy result. Fail-open + timeout-bounded so it can't stall the daily watchdog.

**Tech Stack:** Python, pytest, pandas, yfinance (via `data.py`), `concurrent.futures`.

## Global Constraints

- Python; tests via `python3 -m pytest <path> -v`. State-isolated by the autouse `_isolate_persistent_state` fixture; never write real `.cache` in tests.
- The CANSLIM `screener` strategy and its universe are **unchanged**. `strategies.py` / `investor_agent.py` / `watchdog.py` are **unchanged**.
- Strategy result schema (verbatim, unchanged): `{"strategy": name, "generated_at": iso, "rows": [{"ticker": str, "score": float, "rank": int, "factors": dict}]}`. `factors` must include `"track"`.
- Fail-open everywhere: IWV fetch failure → `[]`; per-ticker `.info` failure → drop that ticker; missing field → that gate passes, subject to the ≥1-of-each-signal guard.
- Config values (verbatim): `VS_MIN_PRICE=5.0`, `VS_MIN_MARKET_CAP=300_000_000`, `VS_MIN_DOLLAR_VOLUME=5_000_000`, `VS_PREFILTER_MAX=500`, `VS_FETCH_WORKERS=12`, `VS_TOP_N=20` (kept), `ENSEMBLE_STRATEGY_TIMEOUT_SEC=240`. `VS_WEIGHTS` is removed. `VS_TRACK_A` / `VS_TRACK_B` dicts as in Task 1.
- `debtToEquity` from yfinance is a percent → `value_fundamentals` divides by 100 to a ratio.
- Per standing instruction: update `README.md`, `docs/system_overview.html`, and the `value_screen` detail flow in `docs/architecture.html` before done (Task 6).
- Commit after every task.

---

### Task 1: Config knobs + `value_fundamentals.py`

**Files:**
- Modify: `config.py` (the `# ── Value+Quality screen + ensemble ──` block, ~lines 417-426)
- Create: `value_fundamentals.py`
- Test: `tests/test_config_flags.py` (update), `tests/test_value_fundamentals.py` (create)

**Interfaces:**
- Produces: the config constants above; `value_fundamentals.Fundamentals` (frozen dataclass) and `value_fundamentals.from_info(ticker: str, info: dict) -> Fundamentals`.

- [ ] **Step 1: Update config tests for the new knobs**

In `tests/test_config_flags.py`, replace the body of `test_ensemble_config_defaults` with:

```python
def test_ensemble_config_defaults():
    import config
    assert config.VS_MIN_DOLLAR_VOLUME == 5_000_000
    assert config.VS_MIN_PRICE == 5.0
    assert config.VS_MIN_MARKET_CAP == 300_000_000
    assert config.VS_TOP_N == 20
    assert config.VS_PREFILTER_MAX == 500
    assert config.VS_FETCH_WORKERS == 12
    assert config.ENSEMBLE_TOP_N == 4
    assert config.ENSEMBLE_STRATEGIES == ["value", "canslim"]
    assert config.ENSEMBLE_STRATEGY_TIMEOUT_SEC == 240
    assert config.VS_TRACK_A["peg_max"] == 1.0 and config.VS_TRACK_A["pe_max"] == 20.0
    assert config.VS_TRACK_B["ps_max"] == 6.0 and config.VS_TRACK_B["cash_runway_quarters_min"] == 6
    assert not hasattr(config, "VS_WEIGHTS")
```

Also update `test_ensemble_strategy_timeout_default` (if present) to assert `== 240`, or delete it (the assertion above covers it).

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_config_flags.py::test_ensemble_config_defaults -v`
Expected: FAIL (old values / VS_WEIGHTS still present).

- [ ] **Step 3: Edit `config.py`**

Replace the existing value/ensemble block with:

```python
# ── Value screen (two-track Russell 3000) + ensemble ────────────
VS_MIN_PRICE = 5.0
VS_MIN_MARKET_CAP = 300_000_000
VS_MIN_DOLLAR_VOLUME = 5_000_000        # ≈ $5M/day liquidity gate (Knob 1)
VS_PREFILTER_MAX = 500                  # cap survivors sent to fundamentals
VS_FETCH_WORKERS = 12                   # concurrent .info fetches
VS_TOP_N = 20                           # final rows emitted
RUSSELL3000_IWV_URL = ("https://www.ishares.com/us/products/239726/"
                       "ishares-russell-3000-etf/1467271812596.ajax"
                       "?fileType=csv&fileName=IWV_holdings&dataType=fund")
VS_TRACK_A = {"peg_max": 1.0, "pe_max": 20.0, "ev_ebitda_max": 12.0,
              "rev_growth_min": 0.15, "eps_growth_min": 0.10,
              "gross_margin_min": 0.30, "debt_equity_max": 1.0,
              "current_ratio_min": 1.5}
VS_TRACK_B = {"ps_max": 6.0, "rev_growth_min": 0.25, "gross_margin_min": 0.40,
              "debt_equity_max": 1.0, "cash_runway_quarters_min": 6,
              "max_dilution": 0.10}
ENSEMBLE_TOP_N = 4
ENSEMBLE_STRATEGIES = ["value", "canslim"]
ENSEMBLE_CANDIDATES_MAX_AGE_HOURS = 24
ENSEMBLE_STRATEGY_TIMEOUT_SEC = 240     # cold-cache Russell-3000 value run needs room
```

Delete the old `VS_WEIGHTS = {...}` line.

- [ ] **Step 4: Write the failing `from_info` tests**

Create `tests/test_value_fundamentals.py`:

```python
from value_fundamentals import from_info, Fundamentals


def test_from_info_maps_and_normalizes():
    info = {"trailingEps": 3.2, "marketCap": 5e9, "trailingPE": 14.0, "pegRatio": 0.8,
            "enterpriseToEbitda": 9.0, "priceToSalesTrailing12Months": 2.0,
            "revenueGrowth": 0.22, "earningsGrowth": 0.18, "grossMargins": 0.41,
            "operatingMargins": 0.12, "debtToEquity": 80.0, "currentRatio": 2.1,
            "freeCashflow": 4e8, "totalCash": 1e9}
    f = from_info("AAA", info)
    assert f.ticker == "AAA"
    assert f.is_profitable is True
    assert f.pe == 14.0 and f.peg == 0.8 and f.ps == 2.0
    assert abs(f.debt_equity - 0.8) < 1e-9         # percent → ratio
    assert f.gross_margin == 0.41 and f.fcf == 4e8


def test_from_info_unprofitable_and_missing():
    f = from_info("BBB", {"netIncomeToCommon": -2e8, "priceToSalesTrailing12Months": 5.0})
    assert f.is_profitable is False
    assert f.ps == 5.0
    assert f.pe is None and f.peg is None and f.debt_equity is None  # absent → None


def test_from_info_empty_does_not_crash():
    f = from_info("CCC", {})
    assert f.is_profitable is False and f.market_cap is None
```

- [ ] **Step 5: Run to verify they fail**

Run: `python3 -m pytest tests/test_value_fundamentals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'value_fundamentals'`

- [ ] **Step 6: Implement `value_fundamentals.py`**

```python
"""Normalize a raw yfinance .info dict into a typed, missing-aware Fundamentals
record. The ONE place that knows yfinance's key names + None/NaN handling, so
the rest of the value screen sees clean fields. Pure — no network."""
from dataclasses import dataclass
from typing import Optional


def _num(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
        return float(v)
    return None


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    market_cap: Optional[float]
    is_profitable: bool
    pe: Optional[float]
    peg: Optional[float]
    ev_ebitda: Optional[float]
    ps: Optional[float]
    rev_growth: Optional[float]
    eps_growth: Optional[float]
    gross_margin: Optional[float]
    op_margin: Optional[float]
    debt_equity: Optional[float]
    current_ratio: Optional[float]
    fcf: Optional[float]
    total_cash: Optional[float]


def from_info(ticker: str, info: dict) -> Fundamentals:
    info = info or {}
    eps = _num(info.get("trailingEps"))
    ni = _num(info.get("netIncomeToCommon"))
    is_profitable = (eps is not None and eps > 0) or (ni is not None and ni > 0)
    d2e = _num(info.get("debtToEquity"))
    if d2e is not None:
        d2e = d2e / 100.0          # yfinance reports a percent (80 = 0.8x)
    return Fundamentals(
        ticker=ticker,
        market_cap=_num(info.get("marketCap")),
        is_profitable=is_profitable,
        pe=_num(info.get("trailingPE")),
        peg=_num(info.get("pegRatio")),
        ev_ebitda=_num(info.get("enterpriseToEbitda")),
        ps=_num(info.get("priceToSalesTrailing12Months")),
        rev_growth=_num(info.get("revenueGrowth")),
        eps_growth=_num(info.get("earningsGrowth")),
        gross_margin=_num(info.get("grossMargins")),
        op_margin=_num(info.get("operatingMargins")),
        debt_equity=d2e,
        current_ratio=_num(info.get("currentRatio")),
        fcf=_num(info.get("freeCashflow")),
        total_cash=_num(info.get("totalCash")),
    )
```

- [ ] **Step 7: Run all Task-1 tests**

Run: `python3 -m pytest tests/test_value_fundamentals.py tests/test_config_flags.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add config.py value_fundamentals.py tests/test_value_fundamentals.py tests/test_config_flags.py
git commit -m "feat(value): config knobs + Fundamentals normalization"
```

---

### Task 2: `discovery.get_russell3000_tickers`

**Files:**
- Modify: `discovery.py` (add the getter + 2 helpers near the other index getters)
- Test: `tests/test_discovery.py` (create or append)

**Interfaces:**
- Consumes: `config.RUSSELL3000_IWV_URL`, existing `_cache_get`/`_cache_set`.
- Produces: `discovery.get_russell3000_tickers() -> list[str]`; `discovery._russell3000_from_csv(text: str) -> list[str]` (pure parser).

- [ ] **Step 1: Write the failing parser tests**

Create `tests/test_discovery.py` (or append if it exists):

```python
import discovery

IWV_CSV = '''iShares Russell 3000 ETF
Fund Holdings as of,"Jun 26, 2026"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%)
AAPL,APPLE INC,Information Technology,Equity,"1,000",3.1
MSFT,MICROSOFT CORP,Information Technology,Equity,"900",2.8
-,USD CASH,Cash and/or Derivatives,Cash,"50",0.2
BRK.B,BERKSHIRE HATHAWAY,Financials,Equity,"800",2.0
'''


def test_russell3000_parser_keeps_equity_tickers():
    syms = discovery._russell3000_from_csv(IWV_CSV)
    assert syms == ["AAPL", "MSFT", "BRK.B"]   # cash row dropped, order preserved


def test_russell3000_parser_failopen_on_garbage():
    assert discovery._russell3000_from_csv("not,a,holdings,file\n1,2,3,4") == []
    assert discovery._russell3000_from_csv("") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_discovery.py -v`
Expected: FAIL — `AttributeError: module 'discovery' has no attribute '_russell3000_from_csv'`

- [ ] **Step 3: Implement in `discovery.py`**

Ensure a module logger exists near the top (`import logging; _log = logging.getLogger(__name__)`) — add it if absent. Then add near the other `get_*_tickers` functions:

```python
def _fetch_text(url: str) -> str:
    """GET a URL's text with a browser UA (past the iShares geo-disclaimer)."""
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _russell3000_from_csv(text: str) -> List[str]:
    """Parse the iShares IWV holdings CSV: skip the fund-info preamble, read the
    holdings table, keep Asset Class == Equity, return the Ticker column. [] on
    any parse failure (fail-open)."""
    import io as _io
    try:
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if ln.lstrip('"').startswith("Ticker,")), None)
        if start is None:
            return []
        df = pd.read_csv(_io.StringIO("\n".join(lines[start:])))
        if "Ticker" not in df.columns:
            return []
        if "Asset Class" in df.columns:
            df = df[df["Asset Class"].astype(str).str.strip().str.lower() == "equity"]
        out: List[str] = []
        for s in df["Ticker"].tolist():
            s = str(s).strip().upper().replace("\xa0", "")
            if s and s != "-" and s.replace(".", "").replace("-", "").isalpha() and 1 <= len(s) <= 6:
                out.append(s)
        return list(dict.fromkeys(out))
    except Exception:
        return []


def get_russell3000_tickers() -> List[str]:
    """Russell 3000 constituents via the iShares IWV holdings CSV. Weekly-cached.
    Fail-open: [] on any HTTP/format failure (caller degrades gracefully)."""
    cached = _cache_get("russell3000", ttl_hours=168)
    if cached:
        return cached
    try:
        syms = _russell3000_from_csv(_fetch_text(config.RUSSELL3000_IWV_URL))
    except Exception as e:
        _log.warning("get_russell3000_tickers: %s", e)
        syms = []
    if len(syms) >= 1000:          # sanity floor — don't cache a partial parse
        _cache_set("russell3000", syms)
    return syms
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_discovery.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add discovery.py tests/test_discovery.py
git commit -m "feat(discovery): Russell 3000 universe via iShares IWV CSV"
```

---

### Task 3: `value_prefilter.py`

**Files:**
- Create: `value_prefilter.py`
- Test: `tests/test_value_prefilter.py`

**Interfaces:**
- Consumes: `config.VS_MIN_PRICE`, `config.VS_MIN_DOLLAR_VOLUME`, `config.VS_PREFILTER_MAX`.
- Produces: `value_prefilter.prefilter(tickers, *, price_fn=None, max_keep=None) -> list[str]`. `price_fn(list[str]) -> dict[str, tuple[float, float]]` maps ticker → `(last_price, avg_dollar_volume)`; defaults to a batched OHLCV implementation, injectable for tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_value_prefilter.py`:

```python
import value_prefilter


def test_prefilter_drops_cheap_and_illiquid(monkeypatch):
    import config
    monkeypatch.setattr(config, "VS_MIN_PRICE", 5.0)
    monkeypatch.setattr(config, "VS_MIN_DOLLAR_VOLUME", 5_000_000)
    data = {
        "OK":    (50.0, 9_000_000),   # passes
        "CHEAP": (3.0, 9_000_000),    # below price floor
        "THIN":  (50.0, 1_000_000),   # below $-vol gate
        "OK2":   (20.0, 6_000_000),   # passes
    }
    out = value_prefilter.prefilter(list(data), price_fn=lambda ts: data)
    assert set(out) == {"OK", "OK2"}
    assert out[0] == "OK"             # higher dollar-volume first


def test_prefilter_caps_at_max_keep():
    data = {f"T{i}": (10.0, (100-i)*1e6) for i in range(10)}
    out = value_prefilter.prefilter(list(data), price_fn=lambda ts: data, max_keep=3)
    assert out == ["T0", "T1", "T2"]   # top-3 by dollar-volume
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_value_prefilter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'value_prefilter'`

- [ ] **Step 3: Implement `value_prefilter.py`**

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_value_prefilter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add value_prefilter.py tests/test_value_prefilter.py
git commit -m "feat(value): stage-0 price/volume pre-filter"
```

---

### Task 4: `value_tracks.py` — the two-track rules (pure)

**Files:**
- Create: `value_tracks.py`
- Test: `tests/test_value_tracks.py`

**Interfaces:**
- Consumes: `config.VS_MIN_MARKET_CAP`, `config.VS_TRACK_A`, `config.VS_TRACK_B`; `value_fundamentals.Fundamentals`.
- Produces: `classify(f) -> 'A'|'B'|None`, `passes(f, track) -> bool`, `score(f, track) -> float`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_value_tracks.py`:

```python
import value_tracks as vt
from value_fundamentals import Fundamentals


def _A(**kw):
    base = dict(ticker="A", market_cap=5e9, is_profitable=True, pe=14.0, peg=0.8,
                ev_ebitda=9.0, ps=2.0, rev_growth=0.2, eps_growth=0.15, gross_margin=0.4,
                op_margin=0.1, debt_equity=0.5, current_ratio=2.0, fcf=4e8, total_cash=1e9)
    base.update(kw); return Fundamentals(**base)


def _B(**kw):
    base = dict(ticker="B", market_cap=2e9, is_profitable=False, pe=None, peg=None,
                ev_ebitda=None, ps=4.0, rev_growth=0.4, eps_growth=None, gross_margin=0.5,
                op_margin=-0.1, debt_equity=0.3, current_ratio=None, fcf=-2e8, total_cash=5e9)
    base.update(kw); return Fundamentals(**base)


def test_classify_routes_by_profitability_and_cap():
    assert vt.classify(_A()) == "A"
    assert vt.classify(_B()) == "B"
    assert vt.classify(_A(market_cap=1e8)) is None    # below cap floor


def test_track_a_accepts_and_each_gate_rejects():
    assert vt.passes(_A(), "A") is True
    assert vt.passes(_A(peg=1.5), "A") is False
    assert vt.passes(_A(pe=30), "A") is False
    assert vt.passes(_A(rev_growth=0.05), "A") is False
    assert vt.passes(_A(eps_growth=0.0), "A") is False
    assert vt.passes(_A(gross_margin=0.2), "A") is False
    assert vt.passes(_A(debt_equity=2.0), "A") is False
    assert vt.passes(_A(current_ratio=1.0), "A") is False
    assert vt.passes(_A(fcf=-1.0), "A") is False


def test_track_b_accepts_and_each_gate_rejects():
    assert vt.passes(_B(), "B") is True                 # fcf -2e8, cash 5e9 → runway 100q
    assert vt.passes(_B(ps=8.0), "B") is False
    assert vt.passes(_B(rev_growth=0.1), "B") is False
    assert vt.passes(_B(gross_margin=0.3), "B") is False
    assert vt.passes(_B(debt_equity=2.0), "B") is False
    assert vt.passes(_B(total_cash=1e7), "B") is False  # runway < 6q


def test_failopen_requires_a_signal_of_each_class():
    # all-None shell: no cheap/growth/solvency signal present → reject
    shell = Fundamentals(ticker="X", market_cap=5e9, is_profitable=True, pe=None, peg=None,
                         ev_ebitda=None, ps=None, rev_growth=None, eps_growth=None,
                         gross_margin=None, op_margin=None, debt_equity=None,
                         current_ratio=None, fcf=None, total_cash=None)
    assert vt.passes(shell, "A") is False


def test_score_orders_cheaper_higher_quality_first():
    cheap = _A(pe=8.0, peg=0.5, rev_growth=0.3, gross_margin=0.5)
    rich = _A(pe=19.0, peg=0.95, rev_growth=0.16, gross_margin=0.31)
    assert vt.score(cheap, "A") > vt.score(rich, "A")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_value_tracks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'value_tracks'`

- [ ] **Step 3: Implement `value_tracks.py`**

```python
"""Two-track selection rules + scoring over Fundamentals. Pure — no I/O.
Track A = profitable 'mispriced bargains'; Track B = unprofitable growth.
Thresholds come from config.VS_TRACK_A / VS_TRACK_B so they're tunable."""
import config
from value_fundamentals import Fundamentals


def classify(f: Fundamentals):
    if f.market_cap is None or f.market_cap < config.VS_MIN_MARKET_CAP:
        return None
    return "A" if f.is_profitable else "B"


def _runway_q(f: Fundamentals):
    """Cash runway in quarters. None if uncomputable; inf if cash-flow positive."""
    if f.fcf is None or f.total_cash is None:
        return None
    burn = max(0.0, -f.fcf) / 4.0
    return float("inf") if burn == 0 else f.total_cash / burn


def _lt(v, th):  # fail-open: missing value does not reject on this gate
    return v is None or v < th
def _gt(v, th):
    return v is None or v > th


def passes(f: Fundamentals, track: str) -> bool:
    if track == "A":
        c = config.VS_TRACK_A
        cheap = any(x is not None for x in (f.peg, f.pe))
        growth = any(x is not None for x in (f.rev_growth, f.eps_growth, f.gross_margin))
        solv = any(x is not None for x in (f.debt_equity, f.current_ratio, f.fcf))
        ok = (_lt(f.peg, c["peg_max"]) and _lt(f.pe, c["pe_max"])
              and _gt(f.rev_growth, c["rev_growth_min"]) and _gt(f.eps_growth, c["eps_growth_min"])
              and _gt(f.gross_margin, c["gross_margin_min"])
              and _lt(f.debt_equity, c["debt_equity_max"]) and _gt(f.current_ratio, c["current_ratio_min"])
              and (f.fcf is None or f.fcf > 0))
        return ok and cheap and growth and solv
    c = config.VS_TRACK_B
    cheap = f.ps is not None
    growth = any(x is not None for x in (f.rev_growth, f.gross_margin))
    runway = _runway_q(f)
    solv = (f.debt_equity is not None) or (runway is not None)
    ok = (_lt(f.ps, c["ps_max"]) and _gt(f.rev_growth, c["rev_growth_min"])
          and _gt(f.gross_margin, c["gross_margin_min"]) and _lt(f.debt_equity, c["debt_equity_max"])
          and (runway is None or runway > c["cash_runway_quarters_min"]))
    return ok and cheap and growth and solv


def score(f: Fundamentals, track: str) -> float:
    parts = []
    if track == "A":
        if f.pe and f.pe > 0: parts.append(min(1.0 / f.pe, 0.2) * 5)
        if f.peg is not None: parts.append(max(0.0, 1.5 - f.peg))
        if f.rev_growth is not None: parts.append(min(f.rev_growth, 1.0))
        if f.gross_margin is not None: parts.append(f.gross_margin)
    else:
        if f.ps and f.ps > 0: parts.append(max(0.0, (6.0 - f.ps) / 6.0))
        if f.rev_growth is not None: parts.append(min(f.rev_growth, 2.0) / 2.0)
        if f.gross_margin is not None: parts.append(f.gross_margin)
        r = _runway_q(f)
        if r is not None and r != float("inf"): parts.append(min(r / 12.0, 1.0))
    return round(sum(parts) / len(parts), 4) if parts else 0.0
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_value_tracks.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add value_tracks.py tests/test_value_tracks.py
git commit -m "feat(value): pure two-track selection rules + scoring"
```

---

### Task 5: `value_screen.py` rewrite (orchestrator)

**Files:**
- Rewrite: `value_screen.py`
- Rewrite: `tests/test_value_screen.py` (the old z-score tests no longer apply)

**Interfaces:**
- Consumes: `discovery.get_russell3000_tickers`, `value_prefilter.prefilter`, `value_fundamentals.from_info`, `value_tracks.{classify,passes,score}`, `data.fetch_info`, `config.{VS_FETCH_WORKERS,VS_TOP_N}`, `strategies.write_strategy_result`.
- Produces: `value_screen.screen(universe, *, price_fn=None, info_fn=None) -> list[dict]`; `value_screen.run(tickers=None) -> list[dict]` (writes `strategies.write_strategy_result("value", rows)`).

- [ ] **Step 1: Rewrite the test file**

Replace the entire contents of `tests/test_value_screen.py` with:

```python
import value_screen


def _info(profitable=True, **kw):
    base = {"marketCap": 5e9, "trailingEps": 3.0 if profitable else -1.0,
            "trailingPE": 14.0, "pegRatio": 0.8, "priceToSalesTrailing12Months": 2.0,
            "revenueGrowth": 0.2, "earningsGrowth": 0.15, "grossMargins": 0.4,
            "debtToEquity": 50.0, "currentRatio": 2.0, "freeCashflow": 4e8, "totalCash": 1e9}
    base.update(kw); return base


def test_screen_emits_twotrack_rows():
    prices = {t: (50.0, 9_000_000) for t in ("AAA", "BBB", "JUNK")}
    infos = {
        "AAA": _info(profitable=True),
        "BBB": _info(profitable=False, priceToSalesTrailing12Months=4.0,
                     revenueGrowth=0.4, grossMargins=0.5, freeCashflow=-2e8, totalCash=5e9),
        "JUNK": _info(profitable=True, pegRatio=3.0, trailingPE=40.0),  # fails track A gates
    }
    rows = value_screen.screen(["AAA", "BBB", "JUNK"],
                               price_fn=lambda ts: prices, info_fn=lambda t: infos[t])
    tickers = [r["ticker"] for r in rows]
    assert "AAA" in tickers and "BBB" in tickers and "JUNK" not in tickers
    assert {r["factors"]["track"] for r in rows} <= {"A", "B"}
    assert rows[0]["rank"] == 1 and all("score" in r for r in rows)


def test_screen_empty_universe_returns_empty():
    assert value_screen.screen([], price_fn=lambda ts: {}, info_fn=lambda t: {}) == []


def test_run_writes_strategy_result(tmp_path, monkeypatch):
    import strategies
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    monkeypatch.setattr(value_screen.discovery, "get_russell3000_tickers", lambda: ["AAA"])
    monkeypatch.setattr(value_screen, "screen", lambda u, **k: [
        {"ticker": "AAA", "score": 1.0, "rank": 1, "factors": {"track": "A"}}])
    rows = value_screen.run()
    assert rows[0]["ticker"] == "AAA"
    assert strategies.load_strategy_results()["value"]["rows"][0]["ticker"] == "AAA"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_value_screen.py -v`
Expected: FAIL (old `screen_value_quality` API; new `screen`/`run` shape differs).

- [ ] **Step 3: Rewrite `value_screen.py`**

```python
"""Value screen — one ensemble strategy. Two-track (profitable / unprofitable-
growth) selection over the Russell 3000, staged cheap→expensive. Thin
orchestrator: universe → prefilter → fundamentals → tracks → rank → write.
Fail-open; bounded by the strategy timeout when run from the daily watchdog."""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor

import config
import discovery
import strategies
import value_tracks
from value_fundamentals import from_info
from value_prefilter import prefilter

_log = logging.getLogger(__name__)


def screen(universe, *, price_fn=None, info_fn=None):
    """Return ranked rows [{ticker, score, rank, factors}] (best first)."""
    from data import fetch_info
    info_fn = info_fn or fetch_info
    survivors = prefilter(universe, price_fn=price_fn)
    if not survivors:
        return []

    def _fund(t):
        try:
            return from_info(t, info_fn(t) or {})
        except Exception:
            return None

    workers = max(1, min(config.VS_FETCH_WORKERS, len(survivors)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        funds = [f for f in ex.map(_fund, survivors) if f is not None]

    picks = {"A": [], "B": []}
    for f in funds:
        tr = value_tracks.classify(f)
        if tr and value_tracks.passes(f, tr):
            picks[tr].append((f, value_tracks.score(f, tr)))
    picks["A"].sort(key=lambda x: -x[1])
    picks["B"].sort(key=lambda x: -x[1])

    rows, i = [], 0
    while (i < len(picks["A"]) or i < len(picks["B"])) and len(rows) < config.VS_TOP_N:
        for tr in ("A", "B"):
            if i < len(picks[tr]) and len(rows) < config.VS_TOP_N:
                f, sc = picks[tr][i]
                rows.append({"ticker": f.ticker, "score": round(float(sc), 4), "factors": {
                    "track": tr, "pe": f.pe, "peg": f.peg, "ps": f.ps,
                    "rev_growth": f.rev_growth, "gross_margin": f.gross_margin,
                    "market_cap": f.market_cap}})
        i += 1
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows


def run(tickers=None):
    universe = list(tickers) if tickers else discovery.get_russell3000_tickers()
    rows = screen(universe)
    strategies.write_strategy_result("value", rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Two-track Russell 3000 value screen")
    ap.add_argument("--tickers", default=None, help="comma-separated; default = Russell 3000")
    args = ap.parse_args()
    rows = run(args.tickers.split(",") if args.tickers else None)
    for r in rows:
        print(f"{r['rank']:>2}  {r['ticker']:<6} [{r['factors']['track']}]  score={r['score']:+.3f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the value_screen + strategies + config suites**

Run: `python3 -m pytest tests/test_value_screen.py tests/test_strategies.py tests/test_config_flags.py -v`
Expected: PASS (strategies registry still maps `value` → `value_screen.run`).

- [ ] **Step 5: Commit**

```bash
git add value_screen.py tests/test_value_screen.py
git commit -m "feat(value): two-track Russell 3000 orchestrator (retires z-score)"
```

---

### Task 6: Full suite + docs + dry-run

**Files:**
- Modify: `README.md`, `docs/system_overview.html`, `docs/architecture.html`

- [ ] **Step 1: Full suite**

Run: `python3 -m pytest -q`
Expected: PASS except the 16 pre-existing `main` failures unrelated to this work (caps/SEPA — confirm the set is unchanged from `main`; this branch adds none).

- [ ] **Step 2: Offline dry-run**

Run:

```bash
python3 -c "
import value_screen
prices={'AAA':(50.0,9e6),'BBB':(50.0,9e6)}
infos={'AAA':{'marketCap':5e9,'trailingEps':3,'trailingPE':14,'pegRatio':0.8,'revenueGrowth':0.2,'earningsGrowth':0.15,'grossMargins':0.4,'debtToEquity':50,'currentRatio':2,'freeCashflow':4e8},
       'BBB':{'marketCap':2e9,'trailingEps':-1,'priceToSalesTrailing12Months':4,'revenueGrowth':0.4,'grossMargins':0.5,'debtToEquity':30,'freeCashflow':-2e8,'totalCash':5e9}}
rows=value_screen.screen(['AAA','BBB'],price_fn=lambda t:prices,info_fn=lambda t:infos[t])
print([(r['ticker'],r['factors']['track'],r['score']) for r in rows])
"
```

Expected: both names emitted with their tracks (A for AAA, B for BBB).

- [ ] **Step 3: Update README**

In the "Ensemble strategy pipeline" / value-screen section, replace the value-screen description with: two-track (profitable Track A / unprofitable-growth Track B) screen over the **Russell 3000** (iShares IWV CSV, weekly-cached, fail-open), staged price/volume pre-filter → parallel fundamentals → track gates → rank; thresholds in `config.VS_TRACK_A/VS_TRACK_B`; liquidity gate `VS_MIN_DOLLAR_VOLUME=$5M`. Note the CANSLIM screener still uses the Wikipedia watchlist.

- [ ] **Step 4: Update the living docs**

In `docs/system_overview.html`, update the `value_screen.py` component card to the two-track Russell-3000 description. In `docs/architecture.html`, replace the `SUB.value` detail-flow nodes/edges with the new pipeline:
`Universe (IWV)` → `Pre-filter (price/$-vol)` → `Fundamentals (parallel .info)` → `Classify A/B` (decision) → `Track gates` (decision) → `Rank + interleave` → `write value.json`.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/system_overview.html docs/architecture.html
git commit -m "docs: two-track Russell 3000 value screen"
```

---

## Self-Review

**Spec coverage:**
- Universe (IWV CSV, weekly cache, fail-open) → Task 2. ✓
- `Fundamentals` normalization (incl. debtToEquity %→ratio) → Task 1. ✓
- Stage-0 pre-filter → Task 3. ✓
- Two-track rules + scoring + ≥1-signal guard → Task 4. ✓
- Orchestrator + parallel fetch + interleave + unchanged schema → Task 5. ✓
- Config knobs (incl. timeout 240, VS_WEIGHTS removed) → Task 1. ✓
- Docs → Task 6. ✓

**Placeholder scan:** none — every step has full code + expected output. ✓

**Type consistency:** `Fundamentals` fields used in Task 4/5 match Task 1's dataclass; `classify/passes/score(f, track)` signatures consistent across Tasks 4–5; `prefilter(tickers, *, price_fn, max_keep)` consistent Tasks 3/5; `screen(universe,*,price_fn,info_fn)` / `run(tickers)` consistent Task 5/tests; result schema `{ticker,score,rank,factors{track,...}}` consistent Tasks 5/6 and the global constraint. ✓
