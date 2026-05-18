# SEPA Take-Profit Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Minervini-style failed-breakout (3-day rule, full exit) and climax (return + range + volume; sell 50%, tighter trail, R-multiple gated off) to the core tranche on top of Phase 1.

**Architecture:** Sidecar `.cache/entry_pivots.json` written by `rebalancer._build_core_targets` for screener-driven entries. New pure-compute helpers `sepa_exits.failed_breakout` and `sepa_exits.climax_check`. `orders.sync_state` initializes a new `climax_fired` field and gates the existing `r_tier_filled` append logic. `watchdog.check_sepa_exits` gains two priority branches (failed-breakout > climax > R-multiple > MA-trail) plus an end-of-pass GC step that prunes the pivot sidecar.

**Tech Stack:** Python 3.9+, pandas, numpy, pytest, pytest-mock.

**Spec:** `docs/superpowers/specs/2026-05-18-sepa-take-profit-phase2-design.md`.

**Verified symbols from the live codebase** (to prevent the Phase 1-style plan bugs):
- `pending_plan.PENDING_PLAN_PATH` (not `PLAN_PATH`)
- `Baseline` is defined in `pending_plan.py`; `from baseline import Baseline` works via re-export but the canonical form `from pending_plan import Baseline` is preferred.
- `Baseline` dataclass field is `news_cursor_at: dt.datetime` (not `captured_at`).
- `orders.OrderIntent` has required positional fields `symbol, notional, side, reason, tranche, client_order_id` plus optional `stop_pct, trail_pct, tier, decision_price, max_price, slice_count`.
- `orders.HALT_PATH = config.HALT_PATH` — monkeypatch `orders.HALT_PATH` for tests.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `config.py` | Modify | Add `SEPA_FAILED_BREAKOUT_WINDOW_DAYS`, `ENTRY_PIVOTS_PATH`, `SEPA_CLIMAX_*`. |
| `screener.py` | Modify | `_detect_base` returns the base `hi` price; `screen_stocks` exposes `base_hi` column on the DataFrame. |
| `sepa_exits.py` | Modify | Add `failed_breakout` and `climax_check` pure-compute helpers. |
| `tests/test_sepa_exits.py` | Modify | Add tests for the two new functions. |
| `orders.py` | Modify | Add `_load_entry_pivots` / `_save_entry_pivots` module-level helpers. Extend `sync_state` to initialize `climax_fired = False` and gate r_tier_filled appends behind `not climax_fired`. |
| `tests/test_orders.py` | Modify | Tests for the new pivot helpers and the sync_state extensions. |
| `rebalancer.py` | Modify | `_build_core_targets` writes `entry_pivots.json` for new screener picks (skips already-held symbols and ETF entries). |
| `tests/test_rebalancer.py` | Modify | Tests for the entry-pivot write path. |
| `watchdog.py` | Modify | Add `_cancel_pending_partials` and `_set_climax_fired` helpers. Extend `check_sepa_exits` with failed-breakout branch (priority 1), climax branch (priority 2), R-multiple gating (`!climax_fired`), MA-trail extended gating (`final_tier OR climax_fired`), end-of-pass GC for entry_pivots.json. |
| `tests/test_watchdog.py` | Modify | Tests for failed-breakout, climax, gating, GC. |
| `tests/test_screener.py` | Modify | Test that `base_hi` is present on the returned DataFrame. |

---

## Task 1: SEPA Phase 2 `config.py` constants

**Files:** `/Users/zl/works/stock/config.py` (insert after the existing `SEPA_MA_HISTORY` line).

- [ ] **Step 1: Add the constants**

Find the existing block ending with `SEPA_MA_HISTORY = "6mo"` in `config.py`. Insert immediately after:

```python

# ── SEPA Phase 2 — failed-breakout ──────────────────────────────
SEPA_FAILED_BREAKOUT_WINDOW_DAYS = 3
ENTRY_PIVOTS_PATH = os.path.join(os.path.dirname(__file__),
                                  ".cache", "entry_pivots.json")

# ── SEPA Phase 2 — climax detection ─────────────────────────────
SEPA_CLIMAX_RETURN_LOOKBACK = 8
SEPA_CLIMAX_RETURN_THRESHOLD = 0.25
SEPA_CLIMAX_RANGE_LOOKBACK = 20
SEPA_CLIMAX_RANGE_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_LOOKBACK = 20
SEPA_CLIMAX_VOLUME_MULTIPLIER = 2.0
SEPA_CLIMAX_VOLUME_RECENT_DAYS = 3
SEPA_CLIMAX_TRAIL_PCT = 0.06       # 6% — half of default core trail
```

- [ ] **Step 2: Sanity-check import**

```bash
cd /Users/zl/works/stock && python3 -c "
import config
assert config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS == 3
assert config.SEPA_CLIMAX_RETURN_LOOKBACK == 8
assert config.SEPA_CLIMAX_TRAIL_PCT == 0.06
assert 'entry_pivots.json' in config.ENTRY_PIVOTS_PATH
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add SEPA Phase 2 config constants"
```

---

## Task 2: `screener.py` — expose `base_hi`

**Files:**
- Modify: `/Users/zl/works/stock/screener.py:52-80` (`_detect_base`) and `screener.py:120-134` (`screen_stocks` row dict construction).
- Modify: `/Users/zl/works/stock/tests/test_screener.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_screener.py`:

```python
def test_screen_stocks_returns_base_hi_column():
    """base_hi (price ceiling of detected base) is present on the result df."""
    import pandas as pd
    import screener as sc

    # Build OHLCV with a clear base: 16 weekly closes tight around 100, last bar at 102.
    n_days = 16 * 5  # ~16 weeks of business days
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    base_close_path = [100.0] * (n_days - 1) + [102.0]
    df_ohlcv = pd.DataFrame({
        ("High",  "TEST"): [c + 1 for c in base_close_path],
        ("Low",   "TEST"): [c - 1 for c in base_close_path],
        ("Close", "TEST"): base_close_path,
    }, index=dates)
    df_ohlcv.columns = pd.MultiIndex.from_tuples(df_ohlcv.columns)

    prices = pd.DataFrame({"TEST": base_close_path}, index=dates)

    from unittest.mock import patch
    with patch("screener.fetch_ohlcv", return_value=df_ohlcv), \
         patch("screener.fetch_prices", return_value=prices):
        df = sc.screen_stocks(tickers=["TEST"])

    if df.empty:
        # The combination of thresholds may not pass screening; assert that
        # _detect_base still returns hi via a direct invocation as a fallback.
        weekly = pd.Series(base_close_path, index=dates).resample("W").last().dropna()
        base = sc._detect_base(weekly)
        assert "hi" in base
        assert base["hi"] is None or isinstance(base["hi"], float)
    else:
        assert "base_hi" in df.columns
        assert df.iloc[0]["base_hi"] is None or float(df.iloc[0]["base_hi"]) > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_screener.py::test_screen_stocks_returns_base_hi_column -v
```

Expected: AssertionError on `"hi" in base` (base dict currently has no `hi` key) OR `"base_hi" in df.columns` if the screener row passes.

- [ ] **Step 3: Extend `_detect_base` to expose `hi`**

In `screener.py`, replace the `_detect_base` function (lines 52-80) with:

```python
def _detect_base(weekly_closes: pd.Series) -> dict:
    """Medium base detection: scan from widest to narrowest valid window.

    A window qualifies when:
      depth     = (hi − lo) / hi        ≤ SCREEN_BASE_DEPTH_MAX
      tightness = std(closes) / mean     ≤ SCREEN_TIGHTNESS_PCT_MAX
      width     between SCREEN_BASE_WEEKS_MIN and SCREEN_BASE_WEEKS_MAX

    Returns a dict containing `hi` (the base's price ceiling) whenever a base
    is detected; `hi` is None when no base qualifies.
    """
    n = len(weekly_closes)
    if n < SCREEN_BASE_WEEKS_MIN:
        return {"in_base": False, "base_weeks": 0, "depth": None,
                "tightness": None, "hi": None}

    for w in range(min(n, SCREEN_BASE_WEEKS_MAX), SCREEN_BASE_WEEKS_MIN - 1, -1):
        window = weekly_closes.iloc[-w:]
        hi = window.max()
        lo = window.min()
        if hi == 0:
            continue
        depth = (hi - lo) / hi
        tightness = window.std() / window.mean() if window.mean() > 0 else 1.0
        if depth <= SCREEN_BASE_DEPTH_MAX and tightness <= SCREEN_TIGHTNESS_PCT_MAX:
            return {
                "in_base": True,
                "base_weeks": w,
                "depth": float(depth),
                "tightness": float(tightness),
                "hi": float(hi),
            }

    return {"in_base": False, "base_weeks": 0, "depth": None,
            "tightness": None, "hi": None}
```

- [ ] **Step 4: Extend `screen_stocks` row dict to surface `base_hi`**

In `screener.py`, find the row dict construction inside the loop (lines 123-134) and replace with:

```python
            rows.append({
                "ticker": t,
                "price": price,
                "rs_score": rs,
                "adr": adr,
                "above_ema_fast": above_ema_fast,
                "above_ema_slow": above_ema_slow,
                "in_base": base["in_base"],
                "base_weeks": base["base_weeks"],
                "base_depth": base["depth"],
                "base_tightness": base["tightness"],
                "base_hi": base.get("hi"),
            })
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_screener.py::test_screen_stocks_returns_base_hi_column -v
```

Expected: PASS.

- [ ] **Step 6: Run full screener test suite for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_screener.py -v --tb=line
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add screener.py tests/test_screener.py
git commit -m "$(cat <<'EOF'
feat: screener exposes base_hi (base price ceiling)

_detect_base now returns the detected base's `hi`, and screen_stocks
surfaces it as a base_hi column on the result DataFrame. Used by SEPA
Phase 2 to persist the entry pivot for failed-breakout detection.
EOF
)"
```

---

## Task 3: `sepa_exits.failed_breakout`

**Files:**
- Modify: `/Users/zl/works/stock/sepa_exits.py` (append after the Phase 1 functions).
- Modify: `/Users/zl/works/stock/tests/test_sepa_exits.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sepa_exits.py`:

```python
# ── failed_breakout ──────────────────────────────────────────────

import datetime as dt


def _closes_with_dates(values, start="2026-05-15"):
    idx = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype=float)


def test_failed_breakout_within_window_close_below_pivot_true():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    # entry day = Mon 2026-05-15; Day 0 close=201, Day 1 close=199 (below)
    closes = _closes_with_dates([201.0, 199.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18),  # Mon of week 2
                           window_days=3) is True


def test_failed_breakout_within_window_all_closes_above_pivot_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    closes = _closes_with_dates([201.0, 202.0, 205.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 19), window_days=3) is False


def test_failed_breakout_window_expired_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-11"}}
    # 4 bars after entry (window=3) — past the window
    closes = _closes_with_dates(
        [201.0, 202.0, 203.0, 204.0, 195.0],   # Day 4 close < pivot
        start="2026-05-11",
    )
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False


def test_failed_breakout_no_pivot_record_false():
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {}  # no pivot for AAPL
    closes = _closes_with_dates([180.0, 170.0], start="2026-05-15")
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False


def test_failed_breakout_insufficient_closes_false():
    """Closes series doesn't reach today → no in-window data → False."""
    from sepa_exits import failed_breakout
    pos = {"symbol": "AAPL"}
    pivots = {"AAPL": {"pivot": 200.0, "entry_date": "2026-05-15"}}
    closes = pd.Series(dtype=float)  # empty
    assert failed_breakout(pos, pivots, closes,
                           today=dt.date(2026, 5, 18), window_days=3) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -k "failed_breakout" -v
```

Expected: ImportError on `from sepa_exits import failed_breakout`.

- [ ] **Step 3: Implement `failed_breakout` in `sepa_exits.py`**

Append to `sepa_exits.py`:

```python
import datetime as _dt


def failed_breakout(position: dict, pivots: dict, closes: pd.Series,
                    *, today: _dt.date,
                    window_days: int = 3) -> bool:
    """Phase 2 — Minervini 3-day failed-breakout rule.

    True iff:
      - `pivots[position['symbol']]` exists with a `pivot` and `entry_date`
      - the count of `closes` index dates strictly after entry_date and
        ≤ today is between 1 and window_days inclusive
      - at least one of those in-window closes is below `pivot`

    The window-day count is observed bars (handles weekends/holidays
    naturally). Returns False on any missing data.
    """
    symbol = position.get("symbol")
    rec = pivots.get(symbol) if symbol else None
    if rec is None:
        return False
    pivot = float(rec["pivot"])
    entry_date = _dt.date.fromisoformat(rec["entry_date"])
    if closes is None or closes.empty:
        return False

    # In-window bars: strictly after entry_date and on/before today.
    idx = closes.index
    in_window = closes[(idx.date > entry_date) & (idx.date <= today)]
    if in_window.empty or len(in_window) > window_days:
        return False
    return bool((in_window < pivot).any())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -k "failed_breakout" -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add sepa_exits.py tests/test_sepa_exits.py
git commit -m "$(cat <<'EOF'
feat: sepa_exits.failed_breakout (Minervini 3-day rule)

Returns True iff at least one in-window close is below the entry
pivot. Window is counted in observed bars to handle weekends/holidays
naturally. Pure compute — callers fetch the closes and pivot records.
EOF
)"
```

---

## Task 4: `sepa_exits.climax_check`

**Files:**
- Modify: `/Users/zl/works/stock/sepa_exits.py`.
- Modify: `/Users/zl/works/stock/tests/test_sepa_exits.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sepa_exits.py`:

```python
# ── climax_check ─────────────────────────────────────────────────

def _ohlcv_df(symbol, *, close, high=None, low=None, volume=None, start="2026-01-01"):
    """Build a MultiIndex OHLCV frame matching data.fetch_ohlcv shape."""
    n = len(close)
    idx = pd.date_range(start, periods=n, freq="B")
    if high is None:
        high = [c + 0.5 for c in close]
    if low is None:
        low = [c - 0.5 for c in close]
    if volume is None:
        volume = [1_000_000] * n
    df = pd.DataFrame({
        ("High",   symbol): high,
        ("Low",    symbol): low,
        ("Close",  symbol): close,
        ("Volume", symbol): volume,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_climax_all_three_conditions_true():
    """30% return over 8 days + 3× ADR + 4× volume → climax True."""
    from sepa_exits import climax_check
    # 50 quiet bars (close=100, narrow range, low volume), then 8 wild bars.
    quiet_closes = [100.0] * 50
    quiet_highs  = [100.5] * 50
    quiet_lows   = [99.5] * 50
    quiet_volume = [1_000_000] * 50

    wild_closes  = [102, 105, 108, 112, 116, 121, 126, 130.0]
    wild_highs   = [c + 3 for c in wild_closes]   # ~3× the quiet 1-pt range
    wild_lows    = [c - 3 for c in wild_closes]
    wild_volume  = [4_000_000] * 8                  # 4× the baseline 1M

    df = _ohlcv_df(
        "X",
        close=quiet_closes + wild_closes,
        high=quiet_highs + wild_highs,
        low=quiet_lows + wild_lows,
        volume=quiet_volume + wild_volume,
    )
    assert climax_check(
        df,
        return_lookback=8, return_threshold=0.25,
        range_lookback=20, range_multiplier=2.0,
        volume_lookback=20, volume_multiplier=2.0,
        volume_recent_days=3,
    ) is True


def test_climax_return_only_false():
    """Return high, but range and volume baseline → no climax."""
    from sepa_exits import climax_check
    closes = [100.0] * 50 + [102, 105, 108, 112, 116, 121, 126, 130.0]
    df = _ohlcv_df("X", close=closes)  # default narrow range, flat volume
    assert climax_check(df) is False


def test_climax_range_only_false():
    """Range expanded but return is small."""
    from sepa_exits import climax_check
    # close stays near 100 but daily range widens
    quiet_close = [100.0] * 50
    recent_close = [100.0, 100.5, 99.8, 100.2, 100.6, 100.1, 99.9, 100.3]
    quiet_high = [100.5] * 50
    recent_high = [c + 3 for c in recent_close]
    quiet_low  = [99.5] * 50
    recent_low = [c - 3 for c in recent_close]
    df = _ohlcv_df(
        "X",
        close=quiet_close + recent_close,
        high=quiet_high + recent_high,
        low=quiet_low + recent_low,
    )
    assert climax_check(df) is False


def test_climax_volume_only_false():
    """Volume spiked, but return and range are normal."""
    from sepa_exits import climax_check
    closes = [100.0] * 50 + [100.5, 99.8, 100.2, 100.6, 100.1, 99.9, 100.3, 100.4]
    volume = [1_000_000] * 50 + [4_000_000] * 8
    df = _ohlcv_df("X", close=closes, volume=volume)
    assert climax_check(df) is False


def test_climax_insufficient_data_false():
    """Fewer than 30 bars → not enough history → False."""
    from sepa_exits import climax_check
    df = _ohlcv_df("X", close=[100.0] * 20)
    assert climax_check(df) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -k "climax" -v
```

Expected: ImportError on `from sepa_exits import climax_check`.

- [ ] **Step 3: Implement `climax_check` in `sepa_exits.py`**

Append to `sepa_exits.py`:

```python
def climax_check(ohlcv: pd.DataFrame, *,
                 return_lookback: int = 8,
                 return_threshold: float = 0.25,
                 range_lookback: int = 20,
                 range_multiplier: float = 2.0,
                 volume_lookback: int = 20,
                 volume_multiplier: float = 2.0,
                 volume_recent_days: int = 3) -> bool:
    """Phase 2 — Minervini climax / blow-off detection.

    Returns True iff all three conditions hold:
      1. Cumulative return over `return_lookback` bars ≥ `return_threshold`.
      2. Mean daily range over the LAST `range_lookback` bars
         ≥ `range_multiplier` × mean daily range over the PRIOR `range_lookback`
         bars (i.e. bars [-2L:-L]).
      3. Max volume over the LAST `volume_recent_days` bars
         ≥ `volume_multiplier` × mean volume over the prior `volume_lookback`
         bars EXCLUDING those recent days
         (i.e. bars [-volume_lookback − volume_recent_days : -volume_recent_days]).

    `ohlcv` is the MultiIndex frame returned by `data.fetch_ohlcv`. The single
    ticker is selected from the column index automatically. Returns False on
    insufficient data.
    """
    if ohlcv is None or ohlcv.empty:
        return False
    # Single-ticker selection from the MultiIndex.
    try:
        close = ohlcv["Close"].iloc[:, 0].dropna()
        high  = ohlcv["High"].iloc[:, 0].dropna()
        low   = ohlcv["Low"].iloc[:, 0].dropna()
        volume = ohlcv["Volume"].iloc[:, 0].dropna()
    except (KeyError, IndexError):
        return False

    needed = max(return_lookback + 1,
                 2 * range_lookback,
                 volume_lookback + volume_recent_days)
    if len(close) < needed:
        return False

    # 1. Return
    ret = (float(close.iloc[-1]) / float(close.iloc[-return_lookback - 1])) - 1.0
    if ret < return_threshold:
        return False

    # 2. Range expansion
    daily_range = (high - low).dropna()
    if len(daily_range) < 2 * range_lookback:
        return False
    recent_range = daily_range.iloc[-range_lookback:].mean()
    prior_range  = daily_range.iloc[-2 * range_lookback:-range_lookback].mean()
    if not (prior_range > 0):
        return False
    if recent_range < range_multiplier * prior_range:
        return False

    # 3. Volume spike — baseline EXCLUDES the recent days under test.
    if len(volume) < volume_lookback + volume_recent_days:
        return False
    recent_vol = volume.iloc[-volume_recent_days:].max()
    baseline_vol = volume.iloc[
        -volume_lookback - volume_recent_days : -volume_recent_days
    ].mean()
    if not (baseline_vol > 0):
        return False
    if recent_vol < volume_multiplier * baseline_vol:
        return False

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_sepa_exits.py -k "climax" -v
```

Expected: 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add sepa_exits.py tests/test_sepa_exits.py
git commit -m "$(cat <<'EOF'
feat: sepa_exits.climax_check (return + range + volume triple condition)

True only when all three hold: cumulative return over N days ≥ threshold,
recent ADR ≥ K× of prior baseline, and at least one of the last few days'
volume ≥ K× of the prior baseline (excluding the recent window from the
baseline). Pure compute against the MultiIndex OHLCV shape that
data.fetch_ohlcv returns.
EOF
)"
```

---

## Task 5: `orders._load_entry_pivots` / `_save_entry_pivots` helpers

**Files:**
- Modify: `/Users/zl/works/stock/orders.py` (add helpers near `_load_portfolio_cache`).
- Modify: `/Users/zl/works/stock/tests/test_orders.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── entry pivots sidecar ─────────────────────────────────────────

def test_load_entry_pivots_missing_file_returns_empty(tmp_path, monkeypatch):
    from orders import _load_entry_pivots
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    assert _load_entry_pivots() == {}


def test_save_then_load_entry_pivots_roundtrip(tmp_path, monkeypatch):
    from orders import _load_entry_pivots, _save_entry_pivots
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    data = {"AAPL": {"pivot": 200.5, "entry_date": "2026-05-18"}}
    _save_entry_pivots(data)
    assert _load_entry_pivots() == data


def test_load_entry_pivots_malformed_returns_empty(tmp_path, monkeypatch):
    from orders import _load_entry_pivots
    path = tmp_path / "pivots.json"
    path.write_text("not-json")
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(path))
    assert _load_entry_pivots() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "entry_pivots" -v
```

Expected: ImportError on `from orders import _load_entry_pivots`.

- [ ] **Step 3: Add `ENTRY_PIVOTS_PATH` ref + helpers in `orders.py`**

In `orders.py`, find the existing `HALT_PATH = config.HALT_PATH` line (around line 86). Insert immediately after:

```python
ENTRY_PIVOTS_PATH = config.ENTRY_PIVOTS_PATH
```

Then, after `_save_portfolio_cache` (around line 103-105), append:

```python
def _load_entry_pivots() -> dict:
    """Load .cache/entry_pivots.json (the SEPA Phase 2 sidecar).

    Returns an empty dict when the file is missing or malformed — entry-pivot
    state is purely advisory; corruption should never block a watchdog run.
    """
    if not os.path.exists(ENTRY_PIVOTS_PATH):
        return {}
    try:
        with open(ENTRY_PIVOTS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_entry_pivots(pivots: dict) -> None:
    """Persist the entry-pivot sidecar."""
    os.makedirs(os.path.dirname(ENTRY_PIVOTS_PATH), exist_ok=True)
    with open(ENTRY_PIVOTS_PATH, "w") as f:
        json.dump(pivots, f, indent=2, default=str)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "entry_pivots" -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "feat: orders._load_entry_pivots / _save_entry_pivots helpers"
```

---

## Task 6: `sync_state` — initialize `climax_fired` + gate r_tier_filled

**Files:**
- Modify: `/Users/zl/works/stock/orders.py` (the SEPA position block inside `sync_state`).
- Modify: `/Users/zl/works/stock/tests/test_orders.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders.py`:

```python
# ── sync_state climax_fired ─────────────────────────────────────

def test_sync_state_initializes_climax_fired_false_on_first_seen(tmp_path, monkeypatch):
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=30, avg_entry=100.0, mv=3000.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["climax_fired"] is False


def test_sync_state_preserves_climax_fired_across_runs(tmp_path, monkeypatch):
    """Once set to True (by watchdog._set_climax_fired), sync_state preserves it."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
             "market_value": 1500.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": True},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=15, avg_entry=100.0, mv=1500.0)
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    assert p["climax_fired"] is True


def test_sync_state_does_not_append_r_tier_when_climax_fired_true(tmp_path, monkeypatch):
    """Climax sold 50%; qty drop must NOT trigger r_tier 2R/3R appends."""
    from orders import sync_state

    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "2026-05-10T14:00:00+00:00",
        "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [
            {"symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
             "market_value": 3000.0, "unrealized_pl": 0.0,
             "tranche": "core", "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": True},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    })

    fb = FakeBroker()
    fb.seed_position("AAPL", qty=15, avg_entry=100.0, mv=1500.0)  # climax sold half
    _seed_stop_order(fb, "AAPL", stop_price=92.0)

    snap = sync_state(fb, alerts=[])
    p = snap.positions[0]
    # Without the gate, r_tier_filled would falsely contain "2R" (and maybe "3R").
    assert p["r_tier_filled"] == []
    assert p["climax_fired"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "climax_fired" -v
```

Expected: KeyError on `p["climax_fired"]` (field not yet written by sync_state).

- [ ] **Step 3: Extend `sync_state` to handle `climax_fired`**

In `orders.py`, find the existing SEPA-field block inside the `for p in live:` loop of `sync_state` (Phase 1 inserted it; it currently builds `initial_entry_price`, `initial_qty`, `initial_stop_price`, and `r_tier_filled`). The block ends with `positions.append({...})`.

Two changes:

(a) Initialize / preserve `climax_fired`. Replace the "first sight" branch's `r_tier_filled: list[str] = []` line with:

```python
            r_tier_filled: list[str] = []
            climax_fired = False
```

And replace the "subsequent" branch's `r_tier_filled = list((meta or {}).get("r_tier_filled", []))` line with:

```python
            r_tier_filled = list((meta or {}).get("r_tier_filled", []))
            climax_fired = bool((meta or {}).get("climax_fired", False))
```

(b) Gate the r_tier_filled append loop. Find the current condition:

```python
            if (initial_qty and float(initial_qty) > 0
                    and initial_stop_price is not None
                    and tranche == "core"):
```

Replace with:

```python
            if (initial_qty and float(initial_qty) > 0
                    and initial_stop_price is not None
                    and tranche == "core"
                    and not climax_fired):
```

(c) Write `climax_fired` to the position dict. In the `positions.append({...})` block, add the key after `r_tier_filled`:

```python
        positions.append({
            # ... existing keys ...
            "initial_entry_price": initial_entry_price,
            "initial_qty": initial_qty,
            "initial_stop_price": initial_stop_price,
            "r_tier_filled": r_tier_filled,
            "climax_fired": climax_fired,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "climax_fired" -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Run all sync_state tests for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_orders.py -k "sync_state" -v --tb=line
```

Expected: all green. The existing Phase 1 sync_state tests continue passing because the new field has a sensible default (`False`).

- [ ] **Step 6: Commit**

```bash
git add orders.py tests/test_orders.py
git commit -m "$(cat <<'EOF'
feat: sync_state initializes climax_fired + gates r_tier appends

New core positions get climax_fired=False; runs after climax preserve
the True flag. When climax_fired=True, the qty-drop heuristic for
r_tier_filled appends is bypassed — climax's own 50% partial sell
must not be misread as Phase 1's 2R/3R completion.
EOF
)"
```

---

## Task 7: `rebalancer` writes entry pivots

**Files:**
- Modify: `/Users/zl/works/stock/rebalancer.py` (`_build_core_targets`, lines 25-55).
- Modify: `/Users/zl/works/stock/tests/test_rebalancer.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rebalancer.py`:

```python
def test_rebalancer_writes_entry_pivots_for_screener_picks(tmp_path, monkeypatch):
    """A screener pick that isn't currently held should get an entry_pivots record."""
    import rebalancer, orders, config as cfg
    import datetime as dt
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Stub screener to return one stock pick with a base_hi.
    df = pd.DataFrame([{
        "ticker": "NVDA", "price": 150.0, "rs_score": 90.0, "adr": 0.05,
        "above_ema_fast": True, "above_ema_slow": True,
        "in_base": True, "base_weeks": 8, "base_depth": 0.10,
        "base_tightness": 0.03, "base_hi": 148.0,
    }])
    monkeypatch.setattr("screener.screen_stocks", lambda: df)
    # Stub momentum + macro so they don't fetch network data.
    monkeypatch.setattr("momentum.generate_signals", lambda: {"holdings": [], "holdings_ranked": []})
    monkeypatch.setattr("macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets()

    pivots = orders._load_entry_pivots()
    assert "NVDA" in pivots
    assert pivots["NVDA"]["pivot"] == 148.0
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert pivots["NVDA"]["entry_date"] == today


def test_rebalancer_skips_entry_pivots_for_already_held_screener_picks(tmp_path, monkeypatch):
    """If the symbol is already in the portfolio cache, don't refresh its pivot."""
    import rebalancer, orders, config as cfg
    import json
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Pre-seed portfolio cache showing NVDA already held.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "2026-05-10T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [{"symbol": "NVDA", "shares": 10, "avg_entry": 150.0,
                       "market_value": 1500.0, "unrealized_pl": 0.0,
                       "tranche": "core", "entry_reason": "core rebalance"}],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    }))
    # Pre-seed an older pivot record so we can detect overwrite.
    (tmp_path / "pivots.json").write_text(json.dumps({
        "NVDA": {"pivot": 140.0, "entry_date": "2026-05-01"}
    }))

    df = pd.DataFrame([{
        "ticker": "NVDA", "price": 150.0, "rs_score": 90.0, "adr": 0.05,
        "above_ema_fast": True, "above_ema_slow": True,
        "in_base": True, "base_weeks": 8, "base_depth": 0.10,
        "base_tightness": 0.03, "base_hi": 148.0,
    }])
    monkeypatch.setattr("screener.screen_stocks", lambda: df)
    monkeypatch.setattr("momentum.generate_signals", lambda: {"holdings": [], "holdings_ranked": []})
    monkeypatch.setattr("macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets()

    pivots = orders._load_entry_pivots()
    # The pre-existing pivot record is preserved (no refresh on already-held).
    assert pivots["NVDA"]["pivot"] == 140.0
    assert pivots["NVDA"]["entry_date"] == "2026-05-01"


def test_rebalancer_skips_entry_pivots_for_etf_targets(tmp_path, monkeypatch):
    """ETF entries from momentum (no screener row) get no pivot record."""
    import rebalancer, orders, config as cfg
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    monkeypatch.setattr("screener.screen_stocks", lambda: pd.DataFrame())  # empty
    monkeypatch.setattr("momentum.generate_signals",
                        lambda: {"holdings": [("SPY", 1.0)], "holdings_ranked": [("SPY", 1.0, 1)]})
    monkeypatch.setattr("macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets()
    pivots = orders._load_entry_pivots()
    assert "SPY" not in pivots
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_rebalancer.py -k "entry_pivots" -v
```

Expected: tests fail — `_build_core_targets` doesn't write to entry_pivots.json yet.

- [ ] **Step 3: Extend `_build_core_targets` in `rebalancer.py`**

Find the existing `_build_core_targets` function (around lines 25-55). Replace the screener block (currently the part starting `from screener import screen_stocks`) with:

```python
    # Stock sleeve: top-3 by composite score + entry-pivot persistence (SEPA Phase 2).
    from screener import screen_stocks
    df = screen_stocks()
    if df is not None and not df.empty:
        top = df.head(3)
        per = stock_pct / max(1, len(top))
        # Identify currently-held symbols so we don't refresh existing pivot records.
        cache = orders._load_portfolio_cache()
        held = {p["symbol"] for p in cache.get("positions", [])}
        pivots = orders._load_entry_pivots()
        today_str = dt.datetime.now(dt.timezone.utc).date().isoformat()
        pivots_dirty = False
        import pandas as _pd
        for _, row in top.iterrows():
            sym = row["ticker"]
            targets[sym] = targets.get(sym, 0.0) + per
            if sym in held:
                continue  # Already held — keep the existing pivot record.
            base_hi = row.get("base_hi")
            if base_hi is None or _pd.isna(base_hi):
                continue
            pivots[sym] = {"pivot": float(base_hi), "entry_date": today_str}
            pivots_dirty = True
        if pivots_dirty:
            orders._save_entry_pivots(pivots)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_rebalancer.py -k "entry_pivots" -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Run full rebalancer test suite for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_rebalancer.py -v --tb=line
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add rebalancer.py tests/test_rebalancer.py
git commit -m "$(cat <<'EOF'
feat: rebalancer writes entry_pivots.json for new screener picks

In _build_core_targets, after collecting the top-3 screener picks,
write each one's base_hi to .cache/entry_pivots.json — but only if
the symbol is not currently held (we don't refresh existing pivot
records). ETF entries from momentum get no pivot record.
EOF
)"
```

---

## Task 8: watchdog helpers — `_cancel_pending_partials` + `_set_climax_fired`

**Files:**
- Modify: `/Users/zl/works/stock/watchdog.py` (insert before `check_sepa_exits`).
- Modify: `/Users/zl/works/stock/tests/test_watchdog.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watchdog.py`:

```python
# ── Phase 2 watchdog helpers ─────────────────────────────────────

def test_cancel_pending_partials_removes_sepa_sell_intents(tmp_path, monkeypatch):
    from watchdog import _cancel_pending_partials
    from pending_plan import (PENDING_PLAN_PATH as _, PendingPlan, IntentState, write_plan)
    from pending_plan import Baseline
    from orders import OrderIntent
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))

    write_plan(PendingPlan(
        plan_id="p-1", tranche="core",
        created_at=dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450, vix=14, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 5, 18, 14, 0, 0,
                                                     tzinfo=dt.timezone.utc)),
        intents=[
            IntentState(intent=OrderIntent(
                symbol="AAPL", notional=1000.0, side="sell",
                reason="sepa-2R", tranche="core", client_order_id="c1",
            )),
            IntentState(intent=OrderIntent(
                symbol="AAPL", notional=500.0, side="buy",
                reason="rebalance", tranche="core", client_order_id="c2",
            )),
            IntentState(intent=OrderIntent(
                symbol="NVDA", notional=800.0, side="sell",
                reason="sepa-3R", tranche="core", client_order_id="c3",
            )),
        ],
    ))

    _cancel_pending_partials("AAPL")

    from pending_plan import load_plan
    plan = load_plan()
    syms_reasons = [(s.intent.symbol, s.intent.reason) for s in plan.intents]
    # AAPL sepa-2R removed; AAPL buy preserved (different side); NVDA sepa-3R preserved.
    assert ("AAPL", "sepa-2R") not in syms_reasons
    assert ("AAPL", "rebalance") in syms_reasons
    assert ("NVDA", "sepa-3R") in syms_reasons


def test_cancel_pending_partials_noop_when_no_plan(tmp_path, monkeypatch):
    from watchdog import _cancel_pending_partials
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    _cancel_pending_partials("AAPL")  # must not raise


def test_set_climax_fired_updates_portfolio_cache(tmp_path, monkeypatch):
    from watchdog import _set_climax_fired
    import json

    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "2026-05-18T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0, "equity": 0,
        "positions": [
            {"symbol": "AAPL", "shares": 30, "avg_entry": 100.0,
             "market_value": 3000, "unrealized_pl": 0, "tranche": "core",
             "entry_reason": "core rebalance",
             "stop_order_id": None, "trail_order_id": None,
             "initial_entry_price": 100.0, "initial_qty": 30,
             "initial_stop_price": 92.0, "r_tier_filled": [],
             "climax_fired": False},
        ],
        "tranches": {"core": {"last_rebalance": "2026-05-18"},
                     "aggressive": {"last_rebalance": None}},
    }))

    _set_climax_fired("AAPL")

    with open(tmp_path / "port.json") as f:
        cache = json.load(f)
    assert cache["positions"][0]["climax_fired"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "cancel_pending_partials or set_climax_fired" -v
```

Expected: ImportError on `from watchdog import _cancel_pending_partials, _set_climax_fired`.

- [ ] **Step 3: Implement the helpers in `watchdog.py`**

Insert directly before `def check_sepa_exits(snap, broker)` in `watchdog.py`:

```python
def _cancel_pending_partials(symbol: str) -> None:
    """Drop SEPA-side sell intents on `symbol` from .cache/pending_plan.json.

    Filters to (side=="sell" AND reason.startswith("sepa-")) so we never
    affect rebalance buys or non-SEPA exits. Idempotent: no plan or no
    matching intents → no-op.
    """
    from pending_plan import load_plan, write_plan
    plan = load_plan()
    if plan is None:
        return
    keep = [
        s for s in plan.intents
        if not (s.intent.symbol == symbol
                and s.intent.side == "sell"
                and s.intent.reason.startswith("sepa-"))
    ]
    if len(keep) == len(plan.intents):
        return
    plan.intents = keep
    write_plan(plan)


def _set_climax_fired(symbol: str) -> None:
    """Set climax_fired=True for `symbol` in the portfolio cache."""
    import json as _json
    cache = orders._load_portfolio_cache()
    for p in cache.get("positions", []):
        if p["symbol"] == symbol:
            p["climax_fired"] = True
            break
    with open(orders.PORTFOLIO_PATH, "w") as f:
        _json.dump(cache, f, indent=2, default=str)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "cancel_pending_partials or set_climax_fired" -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "feat: watchdog helpers — _cancel_pending_partials, _set_climax_fired"
```

---

## Task 9: `watchdog.check_sepa_exits` — failed-breakout branch + GC

**Files:**
- Modify: `/Users/zl/works/stock/watchdog.py:266+` (`check_sepa_exits` body).
- Modify: `/Users/zl/works/stock/tests/test_watchdog.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watchdog.py`:

```python
# ── Phase 2 failed-breakout integration ─────────────────────────

def _seed_entry_pivot(tmp_path, monkeypatch, symbol, pivot, entry_date):
    import json
    path = tmp_path / "pivots.json"
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text())
    existing[symbol] = {"pivot": pivot, "entry_date": entry_date}
    path.write_text(json.dumps(existing))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(path))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(path))


def _stub_fetch_ohlcv_closes(monkeypatch, symbol, closes_values, start="2026-05-15"):
    """Stub data.fetch_ohlcv to return a MultiIndex frame with the given closes."""
    import pandas as pd
    n = len(closes_values)
    idx = pd.date_range(start, periods=n, freq="B")
    df = pd.DataFrame({
        ("High",   symbol): [c + 0.5 for c in closes_values],
        ("Low",    symbol): [c - 0.5 for c in closes_values],
        ("Close",  symbol): closes_values,
        ("Volume", symbol): [1_000_000] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv",
                        lambda tickers, period="1y": df)


def test_check_sepa_exits_failed_breakout_full_exit_path(tmp_path, monkeypatch):
    """Day 2 close < pivot within window → cancel partial + submit_exit."""
    from watchdog import check_sepa_exits
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)  # AAPL entry@100, qty=30
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-15")
    # Closes: 100, 98 (Day 1 below pivot)
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 98.0],
                             start="2026-05-15")
    _stub_baseline(monkeypatch)

    # Pretend "today" is 2026-05-18 (so window covers Days 1-3).
    class _FakeNowMod:
        @staticmethod
        def now(tz=None): return dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 98.0)

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2940.0, "unrealized_pl": -60.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)

    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    # The full exit landed in pending_plan with reason "sepa-failed-breakout".
    assert any(s.intent.symbol == "AAPL" and "failed-breakout" in s.intent.reason
               for s in plan.intents)
    assert any("failed-breakout" in line for line in notifications)


def test_check_sepa_exits_failed_breakout_cancels_pending_phase1_partial(tmp_path, monkeypatch):
    """Existing sepa-2R intent on AAPL is removed when failed-breakout fires."""
    from watchdog import check_sepa_exits
    from pending_plan import (PENDING_PLAN_PATH as _, PendingPlan, IntentState,
                              write_plan, load_plan, Baseline)
    from orders import OrderIntent
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-15")
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 98.0],
                             start="2026-05-15")
    _stub_baseline(monkeypatch)

    write_plan(PendingPlan(
        plan_id="p-1", tranche="core",
        created_at=dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(spy=450, vix=14, macro_score=0.0,
                          news_cursor_at=dt.datetime(2026, 5, 18, 14, 0, 0,
                                                     tzinfo=dt.timezone.utc)),
        intents=[IntentState(intent=OrderIntent(
            symbol="AAPL", notional=1000.0, side="sell",
            reason="sepa-2R", tranche="core", client_order_id="c1",
        ))],
    ))

    class _FakeNowMod:
        @staticmethod
        def now(tz=None): return dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 98.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2940.0, "unrealized_pl": -60.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    plan = load_plan()
    # The original sepa-2R intent is gone; only failed-breakout intent remains.
    reasons = [s.intent.reason for s in plan.intents]
    assert "sepa-2R" not in reasons
    assert any("failed-breakout" in r for r in reasons)


def test_check_sepa_exits_failed_breakout_window_expired_skipped(tmp_path, monkeypatch):
    """Day 5 close below pivot → outside 3-day window → no failed-breakout."""
    from watchdog import check_sepa_exits
    import datetime as dt

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))

    _seed_core_position(tmp_path, monkeypatch)
    _seed_entry_pivot(tmp_path, monkeypatch, "AAPL", pivot=99.0,
                      entry_date="2026-05-11")  # 5 trading days ago
    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL",
                             [100.0, 101.0, 102.0, 101.5, 95.0],
                             start="2026-05-11")
    _stub_baseline(monkeypatch)

    class _FakeNowMod:
        @staticmethod
        def now(tz=None): return dt.datetime(2026, 5, 18, 14, 0, 0, tzinfo=tz)
    monkeypatch.setattr("watchdog.dt.datetime", _FakeNowMod)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 95.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 2850.0, "unrealized_pl": -150.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)
    # No failed-breakout because window expired.
    assert not any("failed-breakout" in line for line in notifications)


def test_check_sepa_exits_gc_removes_exited_pivot_entries(tmp_path, monkeypatch):
    """A pivot for a symbol no longer in the portfolio is GC'd at end of pass."""
    from watchdog import check_sepa_exits
    import json

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Two pivot records — one for held AAPL, one for exited NVDA.
    (tmp_path / "pivots.json").write_text(json.dumps({
        "AAPL": {"pivot": 99.0, "entry_date": "2026-05-15"},
        "NVDA": {"pivot": 150.0, "entry_date": "2026-05-10"},  # exited
    }))

    _stub_fetch_ohlcv_closes(monkeypatch, "AAPL", [100.0, 101.0],
                             start="2026-05-15")

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 101.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3030.0, "unrealized_pl": 30.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    pivots = json.loads((tmp_path / "pivots.json").read_text())
    assert "AAPL" in pivots
    assert "NVDA" not in pivots
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "failed_breakout or gc_removes" -v
```

Expected: assertion failures — failed-breakout branch and GC step don't exist yet.

- [ ] **Step 3: Extend `check_sepa_exits` with failed-breakout branch and end-of-pass GC**

In `watchdog.py`, the existing `check_sepa_exits` function (Phase 1, lines 266+) iterates over `snap.by_tranche("core")` and performs R-multiple then MA-trail per position. Two structural changes:

(a) At the **top of the per-position body**, after the `initial_stop_price is None` skip and the `current_price` fetch, **before** the R-multiple block, insert the failed-breakout branch:

```python
        # Phase 2 — 1. Failed-breakout (highest priority)
        try:
            ohlcv = data.fetch_ohlcv([symbol], period=config.SEPA_MA_HISTORY)
            close_series = (ohlcv["Close"][symbol]
                            if symbol in ohlcv["Close"].columns
                            else ohlcv["Close"].iloc[:, 0]).dropna()
        except Exception as e:
            notifications.append(f"⚠ SEPA {symbol}: closes fetch failed: {e}")
            continue

        pivots = orders._load_entry_pivots()
        today_date = dt.datetime.now(dt.timezone.utc).date()
        if sepa_exits.failed_breakout(
            pos, pivots, close_series,
            today=today_date,
            window_days=config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS,
        ):
            _cancel_pending_partials(symbol)
            orders.cancel_position_trailing(symbol, broker=broker)
            orders.submit_exit(symbol, reason="sepa-failed-breakout", broker=broker)
            pivot_price = float(pivots[symbol]["pivot"])
            _sepa_notify(
                f"⚠ SEPA failed-breakout — {symbol}\n"
                f"Recent close ${float(close_series.iloc[-1]):.2f} < entry pivot "
                f"${pivot_price:.2f}; full exit triggered.",
                notifications,
            )
            continue
```

(b) At the **very end of the function**, before `return notifications`, insert the GC step:

```python
    # Phase 2 — end-of-pass GC: drop pivot records for symbols no longer held.
    held_symbols = {p["symbol"] for p in snap.by_tranche("core")}
    pivots_all = orders._load_entry_pivots()
    pruned = {k: v for k, v in pivots_all.items() if k in held_symbols}
    if len(pruned) != len(pivots_all):
        orders._save_entry_pivots(pruned)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "failed_breakout or gc_removes" -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Run all watchdog tests to confirm Phase 1 didn't regress**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -v --tb=line
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "$(cat <<'EOF'
feat: check_sepa_exits — failed-breakout branch + entry-pivot GC

Top priority in the per-position loop: check entry_pivots.json + recent
closes against the failed-breakout rule. On fire: cancel pending SEPA
partials, cancel trailing, submit_exit. End of pass: prune pivot
records whose symbol is no longer in the core tranche.
EOF
)"
```

---

## Task 10: `watchdog.check_sepa_exits` — climax branch + Phase 1 gating

**Files:**
- Modify: `/Users/zl/works/stock/watchdog.py` (`check_sepa_exits`).
- Modify: `/Users/zl/works/stock/tests/test_watchdog.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watchdog.py`:

```python
# ── Phase 2 climax integration ───────────────────────────────────

def _stub_fetch_ohlcv_full(monkeypatch, symbol, *,
                           close, high=None, low=None, volume=None,
                           start="2026-01-01"):
    import pandas as pd
    n = len(close)
    idx = pd.date_range(start, periods=n, freq="B")
    df = pd.DataFrame({
        ("High",   symbol): high or [c + 0.5 for c in close],
        ("Low",    symbol): low or [c - 0.5 for c in close],
        ("Close",  symbol): close,
        ("Volume", symbol): volume or [1_000_000] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    monkeypatch.setattr("data.fetch_ohlcv",
                        lambda tickers, period="1y": df)


def _climax_ohlcv(symbol):
    """Build OHLCV that satisfies all three climax conditions."""
    quiet_close = [100.0] * 50
    wild_close  = [102, 105, 108, 112, 116, 121, 126, 130.0]
    closes = quiet_close + wild_close
    quiet_high  = [100.5] * 50
    wild_high   = [c + 3 for c in wild_close]
    quiet_low   = [99.5] * 50
    wild_low    = [c - 3 for c in wild_close]
    quiet_vol   = [1_000_000] * 50
    wild_vol    = [4_000_000] * 8
    return dict(close=closes,
                high=quiet_high + wild_high,
                low=quiet_low + wild_low,
                volume=quiet_vol + wild_vol)


def test_check_sepa_exits_climax_sells_half_and_tightens_trail(tmp_path, monkeypatch):
    """All three climax conditions → sell 50% MV + submit tighter trailing."""
    from watchdog import check_sepa_exits
    from broker import Order

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    _seed_core_position(tmp_path, monkeypatch, shares=30.0,
                        market_value=3900.0,  # MV after run-up
                        r_tier_filled=[], climax_fired=False)
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", **_climax_ohlcv("AAPL"),
                           start="2026-01-01")
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)
    fb.seed_open_order(Order(
        id="trail_old", symbol="AAPL", side="sell", type="trailing_stop",
        qty=30.0, notional=None, status="accepted",
        client_order_id="old-trail", parent_order_id=None,
    ))

    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3900.0, "unrealized_pl": 900.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": "trail_old",
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    notifications = check_sepa_exits(snap, fb)

    # Old trailing cancelled, new one submitted with tighter %.
    assert "trail_old" in fb._canceled
    new_trails = [o for o in fb._submitted if o.type == "trailing_stop"]
    assert len(new_trails) == 1
    # 50% partial sell submitted directly (not pending_plan).
    sell_orders = [o for o in fb._submitted if o.side == "sell" and o.type != "trailing_stop"]
    assert any(o.symbol == "AAPL" for o in sell_orders)
    assert any("climax" in line for line in notifications)


def test_check_sepa_exits_climax_sets_climax_fired_true(tmp_path, monkeypatch):
    """After climax, portfolio.json position has climax_fired=True."""
    from watchdog import check_sepa_exits
    import json

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    _seed_core_position(tmp_path, monkeypatch, shares=30.0, market_value=3900.0)
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", **_climax_ohlcv("AAPL"))
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 30.0, "avg_entry": 100.0,
        "market_value": 3900.0, "unrealized_pl": 900.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [], "climax_fired": False,
    }])
    check_sepa_exits(snap, fb)

    with open(tmp_path / "port.json") as f:
        cache = json.load(f)
    assert cache["positions"][0]["climax_fired"] is True


def test_check_sepa_exits_climax_disables_r_multiple_on_next_run(tmp_path, monkeypatch):
    """With climax_fired=True, R-multiple is gated off even at >2R price."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Use OHLCV that does NOT satisfy climax (return only, no range/vol).
    closes = [100.0] * 50 + [102, 105, 108, 112, 116, 121, 126, 130.0]
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", close=closes)
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 130.0)  # well past 2R target of 116
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
        "market_value": 1950.0, "unrealized_pl": 450.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
        "climax_fired": True,  # already fired
    }])
    notifications = check_sepa_exits(snap, fb)
    # No 2R/3R notification because climax_fired gates R-multiple.
    assert not any("2R hit" in line or "3R hit" in line for line in notifications)


def test_check_sepa_exits_climax_allows_ma_trail_after_fired(tmp_path, monkeypatch):
    """With climax_fired=True and close < 21EMA, full exit fires via MA-trail."""
    from watchdog import check_sepa_exits

    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("config.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily.json"))
    monkeypatch.setattr("config.TELEGRAM_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr("orders.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr("config.ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # OHLCV: rise then crash → climax_check False but ma_trail_should_exit True.
    closes = list(range(89, 111)) + [80.0]
    _stub_fetch_ohlcv_full(monkeypatch, "AAPL", close=closes)
    # Phase 1's MA-trail reads via data.fetch_prices too — stub it equivalently.
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    monkeypatch.setattr("data.fetch_prices",
                        lambda tickers, period="2y":
                            pd.DataFrame({"AAPL": closes}, index=idx))
    _stub_baseline(monkeypatch)

    fb = FakeBroker()
    fb.set_latest_price("AAPL", 80.0)
    snap = _make_snap([{
        "symbol": "AAPL", "shares": 15.0, "avg_entry": 100.0,
        "market_value": 1200.0, "unrealized_pl": -300.0,
        "tranche": "core", "entry_reason": "core rebalance",
        "stop_order_id": None, "trail_order_id": None,
        "initial_entry_price": 100.0, "initial_qty": 30,
        "initial_stop_price": 92.0, "r_tier_filled": [],
        "climax_fired": True,
    }])
    notifications = check_sepa_exits(snap, fb)
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "AAPL"
               and "sepa-21EMA-break" in s.intent.reason
               for s in plan.intents)
    assert any("21EMA" in line for line in notifications)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "climax" -v
```

Expected: 4 tests fail — climax branch and gating extensions don't exist.

- [ ] **Step 3: Extend `check_sepa_exits` — climax branch + Phase 1 gating**

In `watchdog.py`, edit `check_sepa_exits` with three changes:

(a) **Insert the climax branch** directly after the failed-breakout branch (Task 9, step 3a) and before the existing `# 1. R-multiple scale-out` comment:

```python
        # Phase 2 — 2. Climax (only if not already fired)
        if not pos.get("climax_fired"):
            if sepa_exits.climax_check(
                ohlcv,
                return_lookback=config.SEPA_CLIMAX_RETURN_LOOKBACK,
                return_threshold=config.SEPA_CLIMAX_RETURN_THRESHOLD,
                range_lookback=config.SEPA_CLIMAX_RANGE_LOOKBACK,
                range_multiplier=config.SEPA_CLIMAX_RANGE_MULTIPLIER,
                volume_lookback=config.SEPA_CLIMAX_VOLUME_LOOKBACK,
                volume_multiplier=config.SEPA_CLIMAX_VOLUME_MULTIPLIER,
                volume_recent_days=config.SEPA_CLIMAX_VOLUME_RECENT_DAYS,
            ):
                _cancel_pending_partials(symbol)
                orders.cancel_position_trailing(symbol, broker=broker)

                # Sell 50% of CURRENT remaining market value, directly via execute_plan
                # (no slicing — climax is "sell into strength, get out today").
                half_mv = float(pos["market_value"]) * 0.5
                cid = orders._make_cid("core", "sepa-climax", symbol, today_date)
                sell_intent = orders.OrderIntent(
                    symbol=symbol, notional=round(half_mv, 2), side="sell",
                    reason="sepa-climax", tranche="core", client_order_id=cid,
                )
                orders.execute_plan(
                    orders.OrderPlan(buys=[], sells=[sell_intent], holds=[]),
                    broker=broker, reason="sepa-climax",
                )

                # Tighter trailing on the (estimated) remaining qty.
                remaining_qty = float(pos["shares"]) * 0.5
                trail_cid = orders._make_cid("core", "climax-trail", symbol, today_date)
                try:
                    broker.submit_trailing_stop(
                        symbol, qty=remaining_qty,
                        trail_percent=config.SEPA_CLIMAX_TRAIL_PCT,
                        client_order_id=trail_cid,
                    )
                except Exception as e:
                    notifications.append(f"⚠ SEPA {symbol}: climax re-trail failed: {e}")

                _set_climax_fired(symbol)
                _sepa_notify(
                    f"🔥 SEPA climax — {symbol}\n"
                    f"Triple condition met; sold ~50% (~${half_mv:,.0f}) at "
                    f"${current_price:.2f}; trailing tightened to "
                    f"{config.SEPA_CLIMAX_TRAIL_PCT*100:.0f}%; "
                    f"R-multiple scale-out disabled.",
                    notifications,
                )
                continue
```

(b) **Gate the existing R-multiple block** behind `not climax_fired`. Find the existing `# 1. R-multiple scale-out` block (still labelled `1.` from Phase 1) and wrap the entire block (from `action = sepa_exits.next_r_tier_action(...)` through the `continue`) in an `if not pos.get("climax_fired"):` guard. Concretely, replace:

```python
        # 1. R-multiple scale-out
        action = sepa_exits.next_r_tier_action(pos, current_price)
        if action is not None:
            ...
            continue
```

with:

```python
        # Phase 1 — 3. R-multiple scale-out (gated by !climax_fired in Phase 2)
        if not pos.get("climax_fired"):
            action = sepa_exits.next_r_tier_action(pos, current_price)
            if action is not None:
                ...   # existing body unchanged
                continue
```

(c) **Extend the MA-trail gate** from `final_tier ∈ r_tier_filled` to `final_tier ∈ r_tier_filled OR climax_fired`. Find:

```python
        # 2. 21EMA trail (only when final tier already filled)
        final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
        if final_label not in (pos.get("r_tier_filled") or []):
            continue
```

Replace with:

```python
        # Phase 1+2 — 4. 21EMA trail
        # Original gate: final R-tier filled. Phase 2 extends: also active
        # after climax_fired so the remaining 50% has an MA backstop.
        final_label = f"{int(config.SEPA_R_TIERS[-1][0])}R"
        if (final_label not in (pos.get("r_tier_filled") or [])
                and not pos.get("climax_fired")):
            continue
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -k "climax" -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Run all watchdog tests for regressions**

```bash
cd /Users/zl/works/stock && python3 -m pytest tests/test_watchdog.py -v --tb=line
```

Expected: all green. Phase 1 tests still pass — the R-multiple branch's outer gate is `not climax_fired`, which is False on Phase 1 positions (they don't set climax_fired) — equivalent to "always run R-multiple" before Phase 2.

- [ ] **Step 6: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "$(cat <<'EOF'
feat: check_sepa_exits — climax branch + Phase 1 gating

Priority 2 after failed-breakout: when climax_check returns True, sell
50% of current MV directly via execute_plan, cancel and replace the
trailing-stop with SEPA_CLIMAX_TRAIL_PCT (default 6%), and set
climax_fired=True on the portfolio cache. R-multiple is gated by
!climax_fired so it stops scaling after climax; MA-trail's gate is
extended to OR(final_tier_filled, climax_fired) so the remaining 50%
gets the MA backstop.
EOF
)"
```

---

## Task 11: Full-suite verification + smoke

**Files:** none modified; verification only.

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/zl/works/stock && python3 -m pytest --tb=line
```

Expected: all unit tests pass. Tally includes the new tests from Tasks 2, 3, 4, 5, 6, 7, 8, 9, 10. Integration tests (`-m integration`) remain deselected per `pytest.ini`.

- [ ] **Step 2: Smoke-check the import graph**

```bash
cd /Users/zl/works/stock && python3 -c "
import config, sepa_exits, orders, watchdog, rebalancer, screener
assert hasattr(sepa_exits, 'failed_breakout')
assert hasattr(sepa_exits, 'climax_check')
assert hasattr(orders, '_load_entry_pivots')
assert hasattr(orders, '_save_entry_pivots')
assert hasattr(watchdog, '_cancel_pending_partials')
assert hasattr(watchdog, '_set_climax_fired')
print('ok')
"
```

Expected: `ok`.

- [ ] **Step 3: Smoke-check the read-only reporter**

```bash
cd /Users/zl/works/stock && python3 run.py 2>&1 | head -50
```

Expected: report runs to completion without exceptions. No assertions to check — just that the system imports cleanly with all Phase 2 additions.

- [ ] **Step 4: Final commit (only if anything changed during verification)**

If steps 1–3 surfaced any fix, commit it. Otherwise skip — no empty commit.

```bash
git status
# If clean, no commit needed.
```

---

## Self-Review Notes

**Spec coverage:**
- §2 Goals — failed-breakout (Tasks 3, 9), climax (Tasks 4, 10), pivot persistence (Tasks 5, 7), GC (Task 9), config exposure (Task 1), core-only/screener-only (Task 7's `held` filter + ETF skip).
- §3 Architecture — diagram matches Task ordering.
- §4.1 entry_pivots.json sidecar → Task 5 (helpers) + Task 7 (write) + Task 9 (read + GC).
- §4.2 portfolio.json `climax_fired` → Task 6.
- §4.3 `sepa_exits.failed_breakout` + `climax_check` → Tasks 3 + 4.
- §4.4 `check_sepa_exits` rewrite → Tasks 9 + 10.
- §4.5 rebalancer entry-pivot write → Task 7. `screener.screen_stocks` base_hi column → Task 2.
- §4.6 config additions → Task 1.
- §5 priority order → enforced by Task 9 (failed-breakout first, continue) + Task 10 (climax second, R-multiple gated, MA-trail extended).
- §6 edge cases — failed-breakout window expiry (Task 9 test), no pivot record (Task 3 test), climax preserves climax_fired across runs (Task 6 test), climax gates R-multiple (Task 10 test), GC clears exited pivots (Task 9 test), `_cancel_pending_partials` filters by side+reason (Task 8 test).
- §7 notifications → emitted by Task 9 (failed-breakout) + Task 10 (climax).
- §8 testing — every named test is present across Tasks 3, 4, 6, 7, 8, 9, 10.
- §10 open questions — none added.

**Placeholder scan:** no TBD/TODO/placeholder. Every code step shows the full code an engineer would paste.

**Type consistency:**
- `sepa_exits.failed_breakout(position, pivots, closes, *, today, window_days=3)` — defined in Task 3, called identically in Task 9.
- `sepa_exits.climax_check(ohlcv, *, return_lookback=..., ...)` — defined in Task 4, called identically in Task 10.
- `orders._load_entry_pivots() -> dict` / `_save_entry_pivots(d)` — defined in Task 5, called in Task 7 (rebalancer) and Task 9 (watchdog GC).
- `watchdog._cancel_pending_partials(symbol)` / `_set_climax_fired(symbol)` — defined in Task 8, called in Tasks 9 (failed-breakout) and 10 (climax).
- `config.SEPA_FAILED_BREAKOUT_WINDOW_DAYS`, `config.ENTRY_PIVOTS_PATH`, `config.SEPA_CLIMAX_*`, `config.SEPA_CLIMAX_TRAIL_PCT` — defined in Task 1, consumed in Tasks 3, 4, 7, 9, 10.
- `pending_plan.PENDING_PLAN_PATH` (verified — not `PLAN_PATH`).
- `pending_plan.Baseline.news_cursor_at` (verified — not `captured_at`).
- `Baseline` imported from `pending_plan` (Phase 1 plan-bug fix carried forward).
- `orders.OrderIntent` constructed with all required positional fields (`symbol, notional, side, reason, tranche, client_order_id`) in Tasks 8, 10.
