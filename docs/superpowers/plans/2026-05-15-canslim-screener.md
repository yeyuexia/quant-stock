# CANSLIM Technical Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Magic Formula screener in `screener.py` with a CANSLIM-style technical screener that filters on RS Rating, ADR, EMA system, and medium-complexity base pattern detection.

**Architecture:** Extend `data.py` with `fetch_ohlcv()` (per-ticker cached OHLCV download), then rewrite `screener.py` with five internal helpers (`_compute_adr`, `_compute_rs_raw`, `_compute_ema_score`, `_detect_base`, and the public `screen_stocks`). Update `config.py` to replace old Magic Formula thresholds with CANSLIM ones, update `quant/applier.py` and `quant/trigger_prompt.md` to reflect the new overrideable keys, and update `run.py` display.

**Tech Stack:** yfinance, pandas, numpy, pytest (existing test suite in `tests/`)

---

## File Map

| File | Action | What changes |
|------|--------|--------------|
| `data.py` | Modify | Add `fetch_ohlcv(tickers, period)` |
| `screener.py` | Replace | Full rewrite: CANSLIM helpers + `screen_stocks()` |
| `config.py` | Modify | Remove `SCREEN_MAX_PE/MIN_ROE/MAX_DEBT_EQUITY`, add `SCREEN_MIN_RS/MIN_ADR_PCT/EMA_MIN_SCORE/BASE_LOOKBACK_WEEKS/BASE_MAX_DEPTH_PCT` |
| `quant/applier.py` | Modify | Swap screener keys in `_HIGH_RISK_KEYS` |
| `quant/trigger_prompt.md` | Modify | Update high-risk allowlist |
| `run.py` | Modify | `run_stock_screener()`: new column display |
| `tests/test_screener.py` | Create | Unit + integration tests for new screener |
| `tests/test_signal_rank.py` | Modify | Drop pe/roe column assertions, add new column checks |

---

## Task 1: Add `fetch_ohlcv()` to `data.py`

**Files:**
- Modify: `stock/data.py`
- Create: `stock/tests/test_screener.py` (start here, add to it in Task 2)

- [ ] **Step 1: Write the failing test**

Create `tests/test_screener.py`:

```python
"""Tests for CANSLIM screener and OHLCV data fetcher."""
import pandas as pd
import numpy as np


def test_fetch_ohlcv_returns_dict_of_dataframes():
    from data import fetch_ohlcv
    result = fetch_ohlcv(["AAPL"], period="3mo")
    assert isinstance(result, dict)
    assert "AAPL" in result
    df = result["AAPL"]
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 30


def test_fetch_ohlcv_has_ohlcv_columns():
    from data import fetch_ohlcv
    result = fetch_ohlcv(["AAPL"], period="3mo")
    df = result["AAPL"]
    for col in ("Open", "High", "Low", "Close", "Volume"):
        assert col in df.columns, f"Missing column: {col}"


def test_fetch_ohlcv_multiple_tickers():
    from data import fetch_ohlcv
    result = fetch_ohlcv(["AAPL", "MSFT"], period="3mo")
    assert "AAPL" in result
    assert "MSFT" in result
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /Users/zl/works/stock
python -m pytest tests/test_screener.py::test_fetch_ohlcv_returns_dict_of_dataframes -v
```

Expected: `ImportError` or `AttributeError: module 'data' has no attribute 'fetch_ohlcv'`

- [ ] **Step 3: Implement `fetch_ohlcv()` in `data.py`**

Add after the `fetch_info` function (after line 59):

```python
def fetch_ohlcv(tickers: list, period: str = "1y") -> dict:
    """Fetch OHLCV per ticker. Returns {ticker: DataFrame[Open,High,Low,Close,Volume]}."""
    result = {}
    missing = []
    for t in tickers:
        path = _cache_path(f"ohlcv_{t}_{period}")
        if _is_fresh(path):
            result[t] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            missing.append(t)
    if not missing:
        return result
    raw = yf.download(missing, period=period, auto_adjust=True, progress=False)
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if isinstance(raw.columns, pd.MultiIndex):
        for t in missing:
            try:
                df = raw.xs(t, axis=1, level=1)[cols].dropna(how="all")
                df.to_csv(_cache_path(f"ohlcv_{t}_{period}"))
                result[t] = df
            except KeyError:
                pass
    else:
        t = missing[0]
        df = raw[cols].dropna(how="all")
        df.to_csv(_cache_path(f"ohlcv_{t}_{period}"))
        result[t] = df
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_screener.py::test_fetch_ohlcv_returns_dict_of_dataframes tests/test_screener.py::test_fetch_ohlcv_has_ohlcv_columns tests/test_screener.py::test_fetch_ohlcv_multiple_tickers -v
```

Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add data.py tests/test_screener.py
git commit -m "feat: add fetch_ohlcv() for per-ticker OHLCV caching"
```

---

## Task 2: Update `config.py` — swap screener thresholds

**Files:**
- Modify: `stock/config.py`

- [ ] **Step 1: In `config.py`, replace the Strategy 2 screener constants (lines 141-146)**

Remove:
```python
SCREEN_MIN_MARKET_CAP = 500e6    # $500M+
SCREEN_MAX_MARKET_CAP = 20e9     # <$20B
SCREEN_MAX_PE = 20
SCREEN_MIN_ROE = 0.12
SCREEN_MAX_DEBT_EQUITY = 1.5
SCREEN_TOP_N = 10                # screen top 10, pick 2-3
```

Replace with:
```python
SCREEN_MIN_RS = 70               # RS Rating floor (0–99 percentile vs watchlist)
SCREEN_MIN_ADR_PCT = 0.03        # Average Daily Range ≥ 3% (daily volatility)
SCREEN_EMA_MIN_SCORE = 2         # price above ≥ 2 of (10/21/50-day) EMAs
SCREEN_BASE_LOOKBACK_WEEKS = 8   # consolidation window for base detection
SCREEN_BASE_MAX_DEPTH_PCT = 0.12 # box depth ≤ 12% of the box low
SCREEN_TOP_N = 10                # final ranked candidates returned
```

- [ ] **Step 2: In `config.py`, update `_OVERRIDE_SCHEMA` — remove old screener keys, add new ones**

Remove these three entries from `_OVERRIDE_SCHEMA`:
```python
    "SCREEN_MIN_ROE":            (float, 0.0,  1.0),
    "SCREEN_MAX_PE":             (float, 5.0,  100.0),
    "SCREEN_MAX_DEBT_EQUITY":    (float, 0.0,  10.0),
```

Add these three entries to `_OVERRIDE_SCHEMA` (in the high-risk section, after `MOMENTUM_TOP_N`):
```python
    "SCREEN_MIN_RS":         (float, 0.0,  99.0),
    "SCREEN_MIN_ADR_PCT":    (float, 0.0,  0.15),
    "SCREEN_EMA_MIN_SCORE":  (int,   0,    3),
```

- [ ] **Step 3: Verify config imports cleanly**

```bash
cd /Users/zl/works/stock
python -c "from config import SCREEN_MIN_RS, SCREEN_MIN_ADR_PCT, SCREEN_EMA_MIN_SCORE, SCREEN_BASE_LOOKBACK_WEEKS, SCREEN_BASE_MAX_DEPTH_PCT, SCREEN_TOP_N; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Run config override tests to confirm schema change doesn't break applier**

```bash
python -m pytest tests/test_config_overrides.py tests/test_quant_applier.py -v
```

Expected: all PASS (the old override keys are gone; tests that tested them should no longer exercise them, or they'll need updating — fix any failures before continuing)

- [ ] **Step 5: Commit**

```bash
git add config.py
git commit -m "feat: replace Magic Formula screener thresholds with CANSLIM thresholds in config"
```

---

## Task 3: Rewrite `screener.py`

**Files:**
- Replace: `stock/screener.py`
- Modify: `stock/tests/test_screener.py` (add unit tests for helpers)

- [ ] **Step 1: Add unit tests for the four internal helpers to `tests/test_screener.py`**

Append to `tests/test_screener.py`:

```python
# ── Unit tests for internal helpers (use synthetic DataFrames) ────

def _make_ohlcv(n=60, daily_range=0.05, trend="flat"):
    """Build synthetic OHLCV DataFrame."""
    import numpy as np
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    base = 100.0
    closes = []
    for i in range(n):
        if trend == "up":
            base *= 1.002
        elif trend == "down":
            base *= 0.998
        closes.append(base)
    closes = np.array(closes)
    highs = closes * (1 + daily_range / 2)
    lows = closes * (1 - daily_range / 2)
    opens = closes * 0.999
    vols = np.full(n, 1_000_000)
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=dates,
    )


def test_compute_adr_known_value():
    from screener import _compute_adr
    df = _make_ohlcv(n=30, daily_range=0.10)
    adr = _compute_adr(df, lookback=20)
    # (High - Low) / Close = daily_range each day
    assert abs(adr - 0.10) < 0.001


def test_compute_adr_lookback_respected():
    from screener import _compute_adr
    import numpy as np
    df = _make_ohlcv(n=30, daily_range=0.10)
    # Spike the last 10 rows to a different range
    df2 = df.copy()
    df2.iloc[-10:, df2.columns.get_loc("High")] = df2.iloc[-10:]["Close"] * 1.20
    df2.iloc[-10:, df2.columns.get_loc("Low")] = df2.iloc[-10:]["Close"] * 0.80
    adr10 = _compute_adr(df2, lookback=10)
    assert abs(adr10 - 0.40) < 0.01


def test_compute_ema_score_above_all():
    from screener import _compute_ema_score
    # Strongly trending up → price should be above all 3 EMAs
    df = _make_ohlcv(n=100, trend="up")
    score, a10, a21, a50, *_ = _compute_ema_score(df)
    assert score == 3
    assert a10 and a21 and a50


def test_compute_ema_score_below_all():
    from screener import _compute_ema_score
    df = _make_ohlcv(n=100, trend="down")
    score, a10, a21, a50, *_ = _compute_ema_score(df)
    assert score == 0
    assert not a10 and not a21 and not a50


def test_compute_rs_raw_positive_trend():
    from screener import _compute_rs_raw
    df = _make_ohlcv(n=260, trend="up")
    rs = _compute_rs_raw(df)
    assert rs > 0


def test_compute_rs_raw_nan_when_insufficient_data():
    from screener import _compute_rs_raw
    df = _make_ohlcv(n=100, trend="up")  # < 252 rows
    rs = _compute_rs_raw(df)
    assert np.isnan(rs)


def test_detect_base_tight_consolidation():
    from screener import _detect_base
    # Flat price + declining volume = in_base
    df = _make_ohlcv(n=100, daily_range=0.02)
    # Volume declining in second half
    mid = len(df) // 2
    df.iloc[mid:, df.columns.get_loc("Volume")] = 500_000
    result = _detect_base(df, lookback_weeks=8, max_depth_pct=0.12)
    assert result["in_base"] is True
    assert result["vol_contraction"] is True
    assert result["base_depth_pct"] < 0.12


def test_detect_base_wide_box_not_in_base():
    from screener import _detect_base
    # Wide range → not a base
    df = _make_ohlcv(n=100, daily_range=0.10)
    result = _detect_base(df, lookback_weeks=8, max_depth_pct=0.12)
    assert result["in_base"] is False


# ── Integration tests for screen_stocks() ─────────────────────────

def test_screener_output_schema():
    from screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])
    if df is None or df.empty:
        return  # all filtered out — valid outcome
    for col in ("ticker", "price", "rs_rating", "adr_pct", "ema_score",
                "in_base", "vol_contraction", "score", "rank"):
        assert col in df.columns, f"Missing column: {col}"


def test_screener_rank_starts_at_1():
    from screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])
    if df is None or df.empty:
        return
    assert df.iloc[0]["rank"] == 1


def test_screener_rs_rating_in_range():
    from screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"])
    if df is None or df.empty:
        return
    assert (df["rs_rating"] >= 0).all()
    assert (df["rs_rating"] <= 99).all()
```

- [ ] **Step 2: Run tests to confirm failures**

```bash
python -m pytest tests/test_screener.py -k "compute_adr or compute_ema or compute_rs or detect_base or screener_output_schema or screener_rank or rs_rating_in_range" -v
```

Expected: `ImportError` — `_compute_adr` etc. not found yet

- [ ] **Step 3: Rewrite `screener.py`**

Replace the entire file with:

```python
"""
Strategy 2: CANSLIM-style Technical Stock Screener

Multi-pass filter on technical strength, not fundamentals:
  1. ADR >= SCREEN_MIN_ADR_PCT          (daily volatility → opportunity)
  2. EMA score >= SCREEN_EMA_MIN_SCORE  (price above ≥ 2 of 10/21/50-day EMAs)
  3. RS Rating >= SCREEN_MIN_RS         (relative strength vs watchlist universe)
  Base pattern (medium): tight 8-week box ≤ 12% depth + volume contraction.

Composite score weights:
  40% RS Rating | 25% EMA quality | 20% ADR | 15% base quality
"""
from typing import Optional, List
import numpy as np
import pandas as pd
from data import fetch_ohlcv
from config import (
    WATCHLIST,
    SCREEN_MIN_RS,
    SCREEN_MIN_ADR_PCT,
    SCREEN_EMA_MIN_SCORE,
    SCREEN_BASE_LOOKBACK_WEEKS,
    SCREEN_BASE_MAX_DEPTH_PCT,
    SCREEN_TOP_N,
)


def _compute_adr(df: pd.DataFrame, lookback: int = 20) -> float:
    """Average Daily Range = mean((High - Low) / Close) over last `lookback` days."""
    tail = df.tail(lookback)
    if tail.empty:
        return 0.0
    return float(((tail["High"] - tail["Low"]) / tail["Close"]).mean())


def _compute_ema_score(df: pd.DataFrame) -> tuple:
    """Returns (score, above_ema10, above_ema21, above_ema50, ema10, ema21, ema50)."""
    close = df["Close"]
    price = float(close.iloc[-1])
    ema10 = float(close.ewm(span=10, adjust=False).mean().iloc[-1])
    ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    above10, above21, above50 = price > ema10, price > ema21, price > ema50
    return int(above10) + int(above21) + int(above50), above10, above21, above50, ema10, ema21, ema50


def _compute_rs_raw(df: pd.DataFrame) -> float:
    """Weighted 12m momentum: (2×3m + 6m + 12m) / 4. NaN if < 252 bars."""
    close = df["Close"].dropna()
    if len(close) < 252:
        return float("nan")
    price = float(close.iloc[-1])
    ret_3m  = price / float(close.iloc[-63])  - 1 if len(close) >= 63  else float("nan")
    ret_6m  = price / float(close.iloc[-126]) - 1 if len(close) >= 126 else float("nan")
    ret_12m = price / float(close.iloc[-252]) - 1
    pairs = [(w, v) for w, v in ((2, ret_3m), (1, ret_6m), (1, ret_12m)) if not np.isnan(v)]
    if not pairs:
        return float("nan")
    return sum(w * v for w, v in pairs) / sum(w for w, _ in pairs)


def _detect_base(df: pd.DataFrame, lookback_weeks: int = 8, max_depth_pct: float = 0.12) -> dict:
    """
    Medium base pattern detection.
    - Box depth: (max - min) / min of close prices over the window.
    - Volume contraction: avg volume in recent half < avg volume in earlier half.
    - in_base: box_depth <= max_depth_pct AND within 30% of 52-week high.
    """
    lookback_days = lookback_weeks * 5
    default = {"in_base": False, "base_depth_pct": float("nan"),
                "vol_contraction": False, "dist_from_high_pct": float("nan")}
    if len(df) < lookback_days + 5:
        return default

    window = df.tail(lookback_days)
    close = window["Close"]
    box_high, box_low = float(close.max()), float(close.min())
    box_depth_pct = (box_high - box_low) / box_low

    high52 = float(df["High"].tail(252).max())
    dist_from_high = (high52 - float(close.iloc[-1])) / high52

    mid = len(window) // 2
    vol_contraction = float(window["Volume"].iloc[mid:].mean()) < float(window["Volume"].iloc[:mid].mean())

    in_base = box_depth_pct <= max_depth_pct and dist_from_high <= 0.30

    return {
        "in_base": bool(in_base),
        "base_depth_pct": box_depth_pct,
        "vol_contraction": bool(vol_contraction),
        "dist_from_high_pct": dist_from_high,
    }


def screen_stocks(tickers: Optional[List[str]] = None) -> pd.DataFrame:
    """Screen for technically strong stocks. Returns ranked DataFrame."""
    if tickers is None:
        tickers = WATCHLIST

    ohlcv = fetch_ohlcv(tickers, period="1y")
    if not ohlcv:
        return pd.DataFrame()

    rows = []
    for t, df in ohlcv.items():
        if df is None or len(df) < 50:
            continue
        price = float(df["Close"].iloc[-1])
        rs_raw = _compute_rs_raw(df)
        adr_pct = _compute_adr(df)
        ema_score, above10, above21, above50, ema10, ema21, ema50 = _compute_ema_score(df)
        base = _detect_base(df, lookback_weeks=SCREEN_BASE_LOOKBACK_WEEKS,
                            max_depth_pct=SCREEN_BASE_MAX_DEPTH_PCT)
        rows.append({
            "ticker": t,
            "price": price,
            "rs_raw": rs_raw,
            "adr_pct": adr_pct,
            "ema_score": ema_score,
            "above_ema10": above10,
            "above_ema21": above21,
            "above_ema50": above50,
            **base,
        })

    if not rows:
        return pd.DataFrame()

    df_all = pd.DataFrame(rows)

    # RS Rating: percentile rank (0–99) across all tickers with valid rs_raw
    if df_all["rs_raw"].notna().any():
        df_all["rs_rating"] = (
            df_all["rs_raw"].rank(pct=True, na_option="bottom") * 99
        ).round(0).astype(int)
    else:
        df_all["rs_rating"] = 0

    # Multi-pass filter
    mask = (
        (df_all["adr_pct"] >= SCREEN_MIN_ADR_PCT) &
        (df_all["ema_score"] >= SCREEN_EMA_MIN_SCORE) &
        (df_all["rs_rating"] >= SCREEN_MIN_RS)
    )
    candidates = df_all[mask].copy()

    if candidates.empty:
        return pd.DataFrame()

    # Composite score
    def _norm(s: pd.Series) -> pd.Series:
        mn, mx = s.min(), s.max()
        return (s - mn) / (mx - mn + 1e-9)

    base_quality = (
        candidates["in_base"].astype(float) * 0.7 +
        candidates["vol_contraction"].astype(float) * 0.3
    )
    candidates["score"] = (
        0.40 * _norm(candidates["rs_rating"]) +
        0.25 * _norm(candidates["ema_score"]) +
        0.20 * _norm(candidates["adr_pct"]) +
        0.15 * base_quality
    )

    out = candidates.sort_values("score", ascending=False).head(SCREEN_TOP_N).reset_index(drop=True)
    out["rank"] = range(1, len(out) + 1)
    return out
```

- [ ] **Step 4: Run all screener tests**

```bash
python -m pytest tests/test_screener.py -v
```

Expected: all PASS (integration tests may return empty DataFrame if thresholds filter everything — that is handled by early return guards in the test)

- [ ] **Step 5: Run the full existing test suite to check regressions**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: no new failures (some tests may be slow due to yfinance calls)

- [ ] **Step 6: Commit**

```bash
git add screener.py tests/test_screener.py
git commit -m "feat: replace Magic Formula screener with CANSLIM technical screener"
```

---

## Task 4: Update `quant/applier.py` and `quant/trigger_prompt.md`

**Files:**
- Modify: `stock/quant/applier.py`
- Modify: `stock/quant/trigger_prompt.md`

- [ ] **Step 1: In `quant/applier.py`, update `_HIGH_RISK_KEYS` (around line 37-46)**

Remove:
```python
    "SCREEN_MIN_ROE",
    "SCREEN_MAX_PE",
    "SCREEN_MAX_DEBT_EQUITY",
```

Add:
```python
    "SCREEN_MIN_RS",
    "SCREEN_MIN_ADR_PCT",
    "SCREEN_EMA_MIN_SCORE",
```

The updated set should be:
```python
_HIGH_RISK_KEYS = {
    "MOMENTUM_TOP_N",
    "ETF_ALLOCATION_PCT",
    "STOCK_ALLOCATION_PCT",
    "SCREEN_MIN_RS",
    "SCREEN_MIN_ADR_PCT",
    "SCREEN_EMA_MIN_SCORE",
    "MOMENTUM_LOOKBACK_MONTHS",
    "SAFE_HAVEN",
}
```

- [ ] **Step 2: In `quant/trigger_prompt.md`, update the high-risk allowlist (line 57)**

Remove:
```
  - `SCREEN_MIN_ROE`; `SCREEN_MAX_PE`; `SCREEN_MAX_DEBT_EQUITY`
```

Replace with:
```
  - `SCREEN_MIN_RS` (0–99, default 70); `SCREEN_MIN_ADR_PCT` (0.0–0.15, default 0.03); `SCREEN_EMA_MIN_SCORE` (0–3, default 2)
```

- [ ] **Step 3: Run applier tests**

```bash
python -m pytest tests/test_quant_applier.py tests/test_quant_schema.py -v
```

Expected: all PASS

- [ ] **Step 4: Verify old keys are now forbidden (not silently ignored)**

```bash
python -c "
from quant.schema import ProposedChange
from quant.applier import classify_change
c = ProposedChange(key='SCREEN_MAX_PE', current_value=20, proposed_value=18,
    rationale='test', detailed_plan='test', expected_effect='test',
    risk_tier='high', confidence=0.8)
result = classify_change(c)
print('SCREEN_MAX_PE classified as:', result)
assert result == 'forbidden', f'Expected forbidden, got {result}'
print('OK')
"
```

Expected: `SCREEN_MAX_PE classified as: forbidden` / `OK`

- [ ] **Step 5: Commit**

```bash
git add quant/applier.py quant/trigger_prompt.md
git commit -m "feat: update quant applier allowlist for CANSLIM screener thresholds"
```

---

## Task 5: Update `run.py` display

**Files:**
- Modify: `stock/run.py`

- [ ] **Step 1: Replace `run_stock_screener()` function (lines 119–148)**

Replace the entire function with:

```python
def run_stock_screener():
    from config import SCREEN_TOP_N
    section("STRATEGY 2: CANSLIM TECHNICAL SCREEN")
    print("  Filtering for RS Rating, ADR, EMA trend, base pattern...\n")

    df = screen_stocks()
    if df is None or df.empty:
        print("  No candidates passed filters.")
        return df

    table_data = []
    for _, row in df.iterrows():
        base_flag = ("✓" if row["in_base"] else " ") + ("↓" if row["vol_contraction"] else " ")
        dist = row.get("dist_from_high_pct")
        dist_str = f"{dist*100:.0f}%" if dist is not None and not pd.isna(dist) else "N/A"
        table_data.append([
            int(row["rank"]),
            row["ticker"],
            f"${row['price']:.2f}",
            f"{int(row['rs_rating']):2d}",
            f"{row['adr_pct']*100:.1f}%",
            f"{int(row['ema_score'])}/3",
            base_flag.strip() or "-",
            dist_str,
            f"{row['score']:.3f}",
        ])

    print(tabulate(table_data,
                   headers=["#", "Ticker", "Price", "RS", "ADR%", "EMA", "Base", "↓Hi%", "Score"],
                   tablefmt="simple"))
    return df
```

- [ ] **Step 2: Verify run.py imports are clean**

```bash
cd /Users/zl/works/stock
python -c "import run; print('imports OK')"
```

Expected: `imports OK` (no ImportError)

- [ ] **Step 3: Smoke-run the screener section**

```bash
python -c "
import run
run.run_stock_screener()
"
```

Expected: either a table of CANSLIM candidates or "No candidates passed filters."

- [ ] **Step 4: Commit**

```bash
git add run.py
git commit -m "feat: update run.py stock screener display for CANSLIM output columns"
```

---

## Task 6: Update `tests/test_signal_rank.py`

**Files:**
- Modify: `stock/tests/test_signal_rank.py`

The existing `test_screener_output_has_rank_column` test is safe (new screener still has `rank`). Add a guard against stale column names.

- [ ] **Step 1: Update `test_signal_rank.py` — replace the screener test**

Replace:
```python
def test_screener_output_has_rank_column():
    from screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL"])
    if df is None or df.empty:
        return
    assert "rank" in df.columns
    assert df.iloc[0]["rank"] == 1
```

With:
```python
def test_screener_output_has_rank_column():
    from screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL"])
    if df is None or df.empty:
        return
    assert "rank" in df.columns
    assert df.iloc[0]["rank"] == 1
    # New CANSLIM columns present; old Magic Formula columns must not be present
    for new_col in ("rs_rating", "adr_pct", "ema_score", "in_base", "score"):
        assert new_col in df.columns, f"Expected column missing: {new_col}"
    for old_col in ("pe", "roe", "debt_equity", "rev_growth"):
        assert old_col not in df.columns, f"Old Magic Formula column still present: {old_col}"
```

- [ ] **Step 2: Run the updated test**

```bash
python -m pytest tests/test_signal_rank.py -v
```

Expected: all PASS

- [ ] **Step 3: Run full test suite one final time**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: no failures introduced by this branch

- [ ] **Step 4: Commit**

```bash
git add tests/test_signal_rank.py
git commit -m "test: update signal_rank test for CANSLIM screener column contract"
```

---

## Self-Review

### Spec coverage

| Requirement | Task |
|-------------|------|
| Replace (not stack) Magic Formula with CANSLIM | Tasks 2, 3 |
| OHLCV data from data.py | Task 1 |
| RS Rating (percentile rank vs universe) | Task 3 |
| ADR ≥ 3% filter | Task 3 |
| EMA system (10/21/50) filter | Task 3 |
| Medium base pattern (tight box + vol contraction) | Task 3 |
| Remove stale config guards (SCREEN_MAX_PE, etc.) | Task 2 |
| Update quant review subagent allowlist | Task 4 |
| Update run.py display | Task 5 |
| Tests | Tasks 1, 3, 6 |

### Placeholder scan — none found

### Type consistency
- `fetch_ohlcv()` returns `dict` — consumed by `screen_stocks()` as `ohlcv.items()` ✓
- `_compute_ema_score()` returns 7-tuple — unpacked in `screen_stocks()` as `ema_score, above10, above21, above50, ema10, ema21, ema50` ✓
- `_detect_base()` returns `dict` — spread into row with `**base` ✓
- `df_all["rs_rating"]` is `int` — `run.py` formats as `int(row["rs_rating"])` ✓
- `run_stock_screener()` imports `SCREEN_TOP_N` locally — needed for the `df.head()` but actually we call `screen_stocks()` which already applies `head(SCREEN_TOP_N)` internally. The local import is used for the `SCREEN_TOP_N` display in section header if desired; can remove if not used. ✓ (no bug — `df.iterrows()` iterates the already-trimmed result)
