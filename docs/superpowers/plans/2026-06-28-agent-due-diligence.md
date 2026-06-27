# Investor-Agent Due-Diligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the near-blind daily agent pick with a peer-relative, dossier-grounded, bias-controlled pipeline (balanced shortlist → analyst → critic → PM with abstention), emitting picks with advisory buy/stop/take-profit levels + logged reasons.

**Architecture:** A new pure module `quant/agent/dossier.py` precomputes a per-candidate dossier (every number in code, fail-open) plus sector peer-relative z-scores and advisory entry/exit levels. `quant/agent/investor.py::select_candidates` is extended to fetch the whole pool's data concurrently, build dossiers, pick a deterministic balanced+blind shortlist, then run three source-blind LLM stages (analyst bull/bear → critic verification → PM 0–5 picks above a conviction floor), each with a deterministic fallback. Source mix and per-pick reasons are logged.

**Tech Stack:** Python 3.9, pytest, pandas, yfinance (existing `quant/data/market.py`), the local `claude` CLI (existing `_default_llm`).

## Global Constraints

- **No behavior change to the watchdog/downstream.** `buy_candidates.json` picks are enriched **additively**; the watchdog reads only `ticker`. Schema must always include `ticker`.
- **Fail-open everywhere.** Any per-ticker fetch / LLM / parse failure degrades to a deterministic fallback. No path raises into the caller. If every LLM stage fails, the result equals today's rule-rank balanced top-N.
- **Grounding.** Dossiers carry every number; prompts state "Use ONLY the numbers in each dossier; never invent a figure; null → 'unknown'." Output is schema-only JSON; anything else → that stage's fallback. **Price levels are computed in code, never LLM-invented.**
- **Source-blind LLM.** No strategy/source label reaches any LLM prompt (Stages B–D). Balance is enforced structurally in the deterministic shortlist.
- **All LLM stages share one injectable `llm_fn(prompt)->str|None`** (default `_default_llm`); each prompt carries a distinct stage marker (`[TRIAGE]` not used — shortlist is deterministic; markers: `STAGE=ANALYST`, `STAGE=CRITIC`, `STAGE=PM`) so a test fake can branch.
- **Config values (verbatim):** `AGENT_DOSSIER_WORKERS=12`, `AGENT_INCLUDE_NEWS=True`, `AGENT_SHORTLIST_PER_SOURCE=4`, `AGENT_SHORTLIST_N=8`, `AGENT_MAX_PICKS=5`, `AGENT_CONVICTION_FLOOR=50`, `AGENT_PEER_MIN_GROUP=3`, `AGENT_RSI_PERIOD=14`, `AGENT_REL_STRENGTH_LOOKBACK_DAYS=63`, `AGENT_BUY_BAND_ATR=0.5`, `AGENT_STOP_ATR_MULT=1.5`, `AGENT_TARGET_R=2.5`.
- **Existing surfaces to reuse:** `quant/data/market.py::fetch_info(ticker)->dict`, `fetch_ohlcv(tickers, period)->MultiIndex DataFrame (field, ticker)`; `quant/data/fundamentals.py::from_info(ticker, info)->Fundamentals`; `quant/signals/indicators.py::atr(high, low, close, period)`; `quant/signals/sentiment.py::fetch_yf_news(tickers)->List[dict]`, `analyze_news_sentiment(news)->dict`; `quant/agent/investor.py::_merge_pool`, `_default_llm`, `BUY_CANDIDATES_PATH`.
- Commit after each task. New tests live in `tests/test_agent_dossier.py` and extend `tests/test_investor_agent.py`.

---

### Task 1: config knobs

**Files:**
- Modify: `quant/config.py` (near the existing `ENSEMBLE_TOP_N = 4` / `ENSEMBLE_STRATEGY_TIMEOUT_SEC` block, ~line 435)
- Test: `tests/test_config_agent.py` (Create)

**Interfaces:**
- Produces: the 12 `AGENT_*` constants listed in Global Constraints.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_agent.py
import quant.config as config


def test_agent_config_knobs_present():
    assert config.AGENT_DOSSIER_WORKERS == 12
    assert config.AGENT_INCLUDE_NEWS is True
    assert config.AGENT_SHORTLIST_PER_SOURCE == 4
    assert config.AGENT_SHORTLIST_N == 8
    assert config.AGENT_MAX_PICKS == 5
    assert config.AGENT_CONVICTION_FLOOR == 50
    assert config.AGENT_PEER_MIN_GROUP == 3
    assert config.AGENT_RSI_PERIOD == 14
    assert config.AGENT_REL_STRENGTH_LOOKBACK_DAYS == 63
    assert config.AGENT_BUY_BAND_ATR == 0.5
    assert config.AGENT_STOP_ATR_MULT == 1.5
    assert config.AGENT_TARGET_R == 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_agent.py -q`
Expected: FAIL (AttributeError: module has no attribute 'AGENT_DOSSIER_WORKERS')

- [ ] **Step 3: Add the constants**

In `quant/config.py`, after the `ENSEMBLE_*` block, add:

```python
# ── Investor-agent due-diligence ─────────────────────────────────────
AGENT_DOSSIER_WORKERS = 12               # concurrent per-candidate data fetches
AGENT_INCLUDE_NEWS = True                # include news/sentiment in the dossier
AGENT_SHORTLIST_PER_SOURCE = 4           # each source's top-K into the shortlist
AGENT_SHORTLIST_N = 8                    # shortlist cap (union of per-source tops)
AGENT_MAX_PICKS = 5                      # PM picks 0..5 (abstention)
AGENT_CONVICTION_FLOOR = 50              # min confidence (0-100) to buy a name
AGENT_PEER_MIN_GROUP = 3                 # min sector members to use sector z-score
AGENT_RSI_PERIOD = 14
AGENT_REL_STRENGTH_LOOKBACK_DAYS = 63    # ~3 months vs SPY
AGENT_BUY_BAND_ATR = 0.5                 # entry band = price ± this·ATR14
AGENT_STOP_ATR_MULT = 1.5                # stop = buy_low − this·ATR14 (or swing low)
AGENT_TARGET_R = 2.5                     # take-profit = this R-multiple of risk
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_agent.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/config.py tests/test_config_agent.py
git commit -m "feat(agent): config knobs for due-diligence dossier pipeline"
```

---

### Task 2: pure dossier helpers (`_rsi`, `_pct_from`, `_rel_strength`, `_zscore`)

**Files:**
- Create: `quant/agent/dossier.py`
- Test: `tests/test_agent_dossier.py` (Create)

**Interfaces:**
- Produces: `_rsi(close: pd.Series, period: int) -> float|None`, `_pct_from(price, ref) -> float|None`, `_rel_strength(tkr_close: pd.Series, spy_close: pd.Series, lookback: int) -> float|None`, `_zscore(values: list[float|None]) -> list[float|None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_dossier.py
import math
import pandas as pd
import quant.agent.dossier as d


def test_pct_from():
    assert d._pct_from(110.0, 100.0) == pytest.approx(0.10)
    assert d._pct_from(None, 100.0) is None
    assert d._pct_from(110.0, 0) is None          # guard divide-by-zero


def test_rsi_all_gains_is_100():
    s = pd.Series([float(i) for i in range(1, 30)])   # monotonic up
    assert d._rsi(s, 14) == pytest.approx(100.0, abs=1e-6)


def test_rsi_too_short_is_none():
    assert d._rsi(pd.Series([1.0, 2.0]), 14) is None


def test_rel_strength_outperformer_positive():
    tkr = pd.Series([100.0 * 1.02 ** i for i in range(70)])   # +2%/day
    spy = pd.Series([100.0 * 1.01 ** i for i in range(70)])   # +1%/day
    assert d._rel_strength(tkr, spy, 63) > 0


def test_zscore_basic():
    z = d._zscore([1.0, 2.0, 3.0, None])
    assert z[3] is None
    assert z[0] < 0 < z[2]
    assert z[1] == pytest.approx(0.0, abs=1e-9)
```

Add `import pytest` at the top of the test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: FAIL (ModuleNotFoundError: quant.agent.dossier)

- [ ] **Step 3: Implement the helpers**

```python
# quant/agent/dossier.py
"""Pure per-candidate dossier assembly for the investor agent. No network I/O —
all inputs (info dict, OHLCV frames, news, estimates) are passed in, so every
function here is deterministic and unit-testable."""
from typing import Optional

import pandas as pd

from quant.data.fundamentals import from_info


def _pct_from(price: Optional[float], ref: Optional[float]) -> Optional[float]:
    if price is None or ref is None or ref == 0:
        return None
    return price / ref - 1.0


def _rsi(close: "pd.Series", period: int) -> Optional[float]:
    if close is None or len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _rel_strength(tkr_close, spy_close, lookback: int) -> Optional[float]:
    if tkr_close is None or spy_close is None:
        return None
    if len(tkr_close) <= lookback or len(spy_close) <= lookback:
        return None
    t = tkr_close.iloc[-1] / tkr_close.iloc[-lookback - 1] - 1.0
    s = spy_close.iloc[-1] / spy_close.iloc[-lookback - 1] - 1.0
    return float(t - s)


def _zscore(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return [None] * len(values)
    mean = sum(nums) / len(nums)
    var = sum((v - mean) ** 2 for v in nums) / len(nums)
    sd = var ** 0.5
    if sd == 0:
        return [0.0 if isinstance(v, (int, float)) else None for v in values]
    return [((v - mean) / sd) if isinstance(v, (int, float)) else None for v in values]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/agent/dossier.py tests/test_agent_dossier.py
git commit -m "feat(agent): pure dossier math helpers (rsi, rel-strength, zscore)"
```

---

### Task 3: `build_dossier` + `compact_line`

**Files:**
- Modify: `quant/agent/dossier.py`
- Test: `tests/test_agent_dossier.py`

**Interfaces:**
- Consumes: `from_info` (fundamentals), `_pct_from`, `_rsi`, `_rel_strength` (Task 2); `quant/signals/indicators.py::atr`.
- Produces: `build_dossier(ticker, *, info, ohlcv=None, spy_ohlcv=None, news=None, estimates=None) -> dict` with the nested schema from the spec; `compact_line(dossier) -> str`.

- [ ] **Step 1: Write the failing test**

```python
def _ohlcv(prices):
    # build a single-ticker MultiIndex (field, ticker) frame like fetch_ohlcv returns
    idx = pd.RangeIndex(len(prices))
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["X"]])
    data = {("Open", "X"): prices, ("High", "X"): [p * 1.01 for p in prices],
            ("Low", "X"): [p * 0.99 for p in prices], ("Close", "X"): prices,
            ("Volume", "X"): [1e6] * len(prices)}
    return pd.DataFrame(data, index=idx)


def test_build_dossier_fields():
    info = {"sector": "Technology", "trailingPE": 18.0, "priceToSalesTrailing12Months": 4.0,
            "currentPrice": 120.0, "fiftyTwoWeekHigh": 150.0, "fiftyTwoWeekLow": 80.0,
            "recommendationKey": "buy", "targetMeanPrice": 144.0, "numberOfAnalystOpinions": 12,
            "heldPercentInsiders": 0.05}
    prices = [100.0 + i for i in range(250)]
    dos = d.build_dossier("X", info=info, ohlcv=_ohlcv(prices), spy_ohlcv=_ohlcv([100.0]*250),
                          news=None, estimates=None)
    assert dos["ticker"] == "X"
    assert dos["sector"] == "Technology"
    assert dos["valuation"]["pe"] == 18.0
    assert dos["analyst"]["recommendation"] == "buy"
    assert dos["analyst"]["target_upside_pct"] == pytest.approx(144.0/120.0 - 1)
    assert dos["price_action"]["rsi14"] is not None
    assert dos["price_action"]["atr14"] is not None
    assert dos["news"] is None or dos["news"]["count"] == 0


def test_build_dossier_failopen_on_empty_info():
    dos = d.build_dossier("Y", info={}, ohlcv=None, spy_ohlcv=None)
    assert dos["ticker"] == "Y"
    assert dos["valuation"]["pe"] is None
    assert dos["price_action"]["price"] is None


def test_compact_line_contains_ticker_and_key_metrics():
    dos = d.build_dossier("X", info={"trailingPE": 18.0, "currentPrice": 120.0}, ohlcv=None)
    line = d.compact_line(dos)
    assert "X" in line and "PE" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: FAIL (AttributeError: module 'quant.agent.dossier' has no attribute 'build_dossier')

- [ ] **Step 3: Implement `build_dossier` + `compact_line`**

Add to `quant/agent/dossier.py`:

```python
from quant.signals.indicators import atr


def _series(ohlcv, field, ticker):
    """Extract a field Series for `ticker` from a MultiIndex (field, ticker) frame."""
    if ohlcv is None or len(ohlcv) == 0:
        return None
    try:
        sub = ohlcv[field]
    except Exception:
        return None
    if ticker in getattr(sub, "columns", []):
        return sub[ticker].dropna()
    # single-ticker frame may have a lone column
    if hasattr(sub, "columns") and len(sub.columns) == 1:
        return sub.iloc[:, 0].dropna()
    return None


def _last(series):
    return float(series.iloc[-1]) if series is not None and len(series) else None


def build_dossier(ticker, *, info, ohlcv=None, spy_ohlcv=None, news=None, estimates=None) -> dict:
    import quant.config as config
    f = from_info(ticker, info or {})
    close = _series(ohlcv, "Close", ticker)
    high = _series(ohlcv, "High", ticker)
    low = _series(ohlcv, "Low", ticker)
    spy_close = _series(spy_ohlcv, "Close", "SPY") if spy_ohlcv is not None else None

    price = (info or {}).get("currentPrice") or _last(close)
    hi52 = (info or {}).get("fiftyTwoWeekHigh")
    lo52 = (info or {}).get("fiftyTwoWeekLow")
    dma50 = float(close.rolling(50).mean().iloc[-1]) if close is not None and len(close) >= 50 else None
    dma200 = float(close.rolling(200).mean().iloc[-1]) if close is not None and len(close) >= 200 else None
    atr14 = None
    if high is not None and low is not None and close is not None and len(close) > config.AGENT_RSI_PERIOD:
        try:
            atr14 = float(atr(high, low, close, config.AGENT_RSI_PERIOD).iloc[-1])
        except Exception:
            atr14 = None
    swing_low_20 = float(low.tail(20).min()) if low is not None and len(low) >= 20 else None
    swing_high_20 = float(high.tail(20).max()) if high is not None and len(high) >= 20 else None

    tgt = (info or {}).get("targetMeanPrice")
    est = estimates or {}
    news_section = None
    if news is not None:
        from quant.signals.sentiment import analyze_news_sentiment
        sent = analyze_news_sentiment(news) if news else {}
        news_section = {"count": len(news),
                        "sentiment_score": sent.get("score"),
                        "sentiment_label": sent.get("label"),
                        "headlines": [n.get("title", "") for n in news[:3]]}

    return {
        "ticker": ticker,
        "sector": (info or {}).get("sector"),
        "valuation": {"pe": f.pe, "peg": f.peg, "ev_ebitda": f.ev_ebitda, "ps": f.ps,
                      "fcf_yield": (f.fcf / f.market_cap) if (f.fcf is not None and f.market_cap) else None},
        "quality": {"gross_margin": f.gross_margin, "op_margin": f.op_margin,
                    "roe": (info or {}).get("returnOnEquity"), "debt_equity": f.debt_equity,
                    "current_ratio": f.current_ratio, "profitable": f.is_profitable},
        "growth": {"rev_growth": f.rev_growth, "eps_growth": f.eps_growth},
        "estimates": {"revision_trend": est.get("revision_trend"),
                      "up_revisions_90d": est.get("up_revisions_90d"),
                      "down_revisions_90d": est.get("down_revisions_90d"),
                      "surprises": est.get("surprises", [])},
        "price_action": {"price": price,
                         "pct_from_52w_high": _pct_from(price, hi52),
                         "pct_from_52w_low": _pct_from(price, lo52),
                         "pct_vs_50dma": _pct_from(price, dma50),
                         "pct_vs_200dma": _pct_from(price, dma200),
                         "rsi14": _rsi(close, config.AGENT_RSI_PERIOD),
                         "rel_strength_vs_spy_3m": _rel_strength(close, spy_close, config.AGENT_REL_STRENGTH_LOOKBACK_DAYS),
                         "atr14": atr14, "swing_low_20": swing_low_20, "swing_high_20": swing_high_20},
        "analyst": {"recommendation": (info or {}).get("recommendationKey"),
                    "target_upside_pct": _pct_from(tgt, price),
                    "num_analysts": (info or {}).get("numberOfAnalystOpinions")},
        "insider": {"pct_held_insiders": (info or {}).get("heldPercentInsiders")},
        "news": news_section,
        "peer_relative": {"pe_z": None, "ps_z": None, "ev_ebitda_z": None,
                          "rev_growth_z": None, "gross_margin_z": None},
    }


def _fmt(x, pct=False):
    if x is None:
        return "?"
    return f"{x*100:.0f}%" if pct else f"{x:.1f}"


def compact_line(dossier) -> str:
    v, g, pa = dossier["valuation"], dossier["growth"], dossier["price_action"]
    return (f"{dossier['ticker']} PE:{_fmt(v['pe'])} PS:{_fmt(v['ps'])} "
            f"revG:{_fmt(g['rev_growth'], pct=True)} RSI:{_fmt(pa['rsi14'])} "
            f"vs200dma:{_fmt(pa['pct_vs_200dma'], pct=True)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/agent/dossier.py tests/test_agent_dossier.py
git commit -m "feat(agent): build_dossier + compact_line"
```

---

### Task 4: `add_peer_relative` + `suggested_levels`

**Files:**
- Modify: `quant/agent/dossier.py`
- Test: `tests/test_agent_dossier.py`

**Interfaces:**
- Consumes: `_zscore` (Task 2); dossiers from `build_dossier` (Task 3).
- Produces: `add_peer_relative(dossiers: list[dict], *, min_group: int) -> None` (mutates each dossier's `peer_relative`); `suggested_levels(dossier, *, buy_band_atr, stop_atr_mult, target_r) -> dict` returning `{buy_low, buy_high, stop_loss, take_profit}`.

- [ ] **Step 1: Write the failing test**

```python
def _mk(ticker, sector, pe, ps, rev, gm):
    return {"ticker": ticker, "sector": sector,
            "valuation": {"pe": pe, "ps": ps, "ev_ebitda": None},
            "growth": {"rev_growth": rev}, "quality": {"gross_margin": gm},
            "peer_relative": {"pe_z": None, "ps_z": None, "ev_ebitda_z": None,
                              "rev_growth_z": None, "gross_margin_z": None}}


def test_add_peer_relative_sector_group():
    ds = [_mk("A", "Tech", 10, 2, 0.3, 0.5), _mk("B", "Tech", 20, 4, 0.2, 0.4),
          _mk("C", "Tech", 30, 6, 0.1, 0.3)]
    d.add_peer_relative(ds, min_group=3)
    # lower-is-better PE negated: cheapest A gets the HIGHEST pe_z
    assert ds[0]["peer_relative"]["pe_z"] > ds[2]["peer_relative"]["pe_z"]
    # higher-is-better rev_growth: A highest gets highest z
    assert ds[0]["peer_relative"]["rev_growth_z"] > ds[2]["peer_relative"]["rev_growth_z"]


def test_add_peer_relative_small_sector_falls_back_to_pool():
    ds = [_mk("A", "Tech", 10, 2, 0.3, 0.5), _mk("B", "Energy", 20, 4, 0.2, 0.4),
          _mk("C", "Energy", 30, 6, 0.1, 0.3)]
    d.add_peer_relative(ds, min_group=3)   # no sector has 3 → pool-wide
    assert ds[0]["peer_relative"]["pe_z"] is not None


def test_suggested_levels():
    dos = {"price_action": {"price": 100.0, "atr14": 4.0, "swing_low_20": 95.0}}
    lv = d.suggested_levels(dos, buy_band_atr=0.5, stop_atr_mult=1.5, target_r=2.5)
    assert lv["buy_low"] == pytest.approx(98.0)     # 100 - 0.5*4
    assert lv["buy_high"] == pytest.approx(102.0)   # 100 + 0.5*4
    # stop = min(swing_low_20=95, buy_low - 1.5*4 = 92) = 92
    assert lv["stop_loss"] == pytest.approx(92.0)
    # tp = buy_high + 2.5*(buy_high - stop) = 102 + 2.5*10 = 127
    assert lv["take_profit"] == pytest.approx(127.0)


def test_suggested_levels_failopen():
    lv = d.suggested_levels({"price_action": {"price": None, "atr14": None}},
                            buy_band_atr=0.5, stop_atr_mult=1.5, target_r=2.5)
    assert lv == {"buy_low": None, "buy_high": None, "stop_loss": None, "take_profit": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: FAIL (no attribute 'add_peer_relative')

- [ ] **Step 3: Implement**

Add to `quant/agent/dossier.py`:

```python
# (metric path, dossier section, lower-is-better?)
_PEER_METRICS = [("pe_z", "valuation", "pe", True), ("ps_z", "valuation", "ps", True),
                 ("ev_ebitda_z", "valuation", "ev_ebitda", True),
                 ("rev_growth_z", "growth", "rev_growth", False),
                 ("gross_margin_z", "quality", "gross_margin", False)]


def _assign_z(group, zkey, section, metric, lower_better):
    vals = [g[section].get(metric) for g in group]
    zs = _zscore(vals)
    for g, z in zip(group, zs):
        g["peer_relative"][zkey] = (-z if (z is not None and lower_better) else z)


def add_peer_relative(dossiers, *, min_group: int) -> None:
    if not dossiers:
        return
    by_sector = {}
    for dos in dossiers:
        by_sector.setdefault(dos.get("sector"), []).append(dos)
    for zkey, section, metric, lower_better in _PEER_METRICS:
        for sector, group in by_sector.items():
            target = group if (sector is not None and len(group) >= min_group) else None
            if target is None:
                continue
            _assign_z(target, zkey, section, metric, lower_better)
        # pool-wide fallback for dossiers still unscored on this metric
        unscored = [dos for dos in dossiers if dos["peer_relative"][zkey] is None]
        if len(unscored) >= 2:
            _assign_z(unscored, zkey, section, metric, lower_better)


def _round2(x):
    return round(float(x), 2) if x is not None else None


def suggested_levels(dossier, *, buy_band_atr, stop_atr_mult, target_r) -> dict:
    pa = dossier.get("price_action", {})
    price, atr14 = pa.get("price"), pa.get("atr14")
    if price is None or atr14 is None:
        return {"buy_low": None, "buy_high": None, "stop_loss": None, "take_profit": None}
    buy_low = price - buy_band_atr * atr14
    buy_high = price + buy_band_atr * atr14
    vol_stop = buy_low - stop_atr_mult * atr14
    swing = pa.get("swing_low_20")
    stop = min(vol_stop, swing) if swing is not None else vol_stop
    take_profit = buy_high + target_r * (buy_high - stop)
    return {"buy_low": _round2(buy_low), "buy_high": _round2(buy_high),
            "stop_loss": _round2(stop), "take_profit": _round2(take_profit)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_dossier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/agent/dossier.py tests/test_agent_dossier.py
git commit -m "feat(agent): peer-relative z-scores + advisory entry/exit levels"
```

---

### Task 5: `data.fetch_estimates` (revisions + surprises, fail-open)

**Files:**
- Modify: `quant/data/market.py`
- Test: `tests/test_quant_data_sources.py` (extend) or `tests/test_data.py`

**Interfaces:**
- Produces: `fetch_estimates(ticker: str) -> dict` returning `{"revision_trend": "rising"|"falling"|"flat"|None, "up_revisions_90d": int|None, "down_revisions_90d": int|None, "surprises": list[float]}`. Fail-open: returns the all-None/empty shape on any error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data.py  (add)
import quant.data.market as market


def test_fetch_estimates_failopen(monkeypatch):
    # force the yfinance path to raise → fail-open shape
    monkeypatch.setattr("quant.data.market.yf.Ticker",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = market.fetch_estimates("ZZZZ")
    assert out == {"revision_trend": None, "up_revisions_90d": None,
                   "down_revisions_90d": None, "surprises": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_data.py::test_fetch_estimates_failopen -q`
Expected: FAIL (no attribute 'fetch_estimates')

- [ ] **Step 3: Implement**

Add to `quant/data/market.py` (uses the module's existing `yf` import and `_TICKER_TIMEOUT`/`_run_with_timeout` helpers; keep it defensive — yfinance's estimate fields vary by version):

```python
_ESTIMATES_EMPTY = {"revision_trend": None, "up_revisions_90d": None,
                    "down_revisions_90d": None, "surprises": []}


def fetch_estimates(ticker: str) -> dict:
    """Analyst EPS-estimate revision trend + recent earnings-surprise history.
    Fail-open: returns the all-None/empty shape on any error or missing data."""
    def _do():
        t = yf.Ticker(ticker)
        up = dn = None
        try:
            rev = t.eps_revisions            # DataFrame indexed by period
            if rev is not None and not rev.empty:
                up = int(rev.get("upLast30days", rev.iloc[:, 0]).fillna(0).sum())
                dn = int(rev.get("downLast30days", rev.iloc[:, -1]).fillna(0).sum())
        except Exception:
            up = dn = None
        trend = None
        if up is not None and dn is not None:
            trend = "rising" if up > dn else "falling" if dn > up else "flat"
        surprises = []
        try:
            ed = t.get_earnings_dates(limit=8)
            if ed is not None and "Surprise(%)" in ed.columns:
                surprises = [float(x) / 100.0 for x in ed["Surprise(%)"].dropna().head(4)]
        except Exception:
            surprises = []
        return {"revision_trend": trend, "up_revisions_90d": up,
                "down_revisions_90d": dn, "surprises": surprises}
    try:
        return _run_with_timeout(_do, timeout=_TICKER_TIMEOUT)
    except Exception as e:
        _log.warning("fetch_estimates: %s failed: %s", ticker, e)
        return dict(_ESTIMATES_EMPTY)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_data.py::test_fetch_estimates_failopen -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/data/market.py tests/test_data.py
git commit -m "feat(data): fetch_estimates (revision trend + earnings surprises, fail-open)"
```

---

### Task 6: dossier-fetch orchestration + balanced blind shortlist in `investor.py`

**Files:**
- Modify: `quant/agent/investor.py`
- Test: `tests/test_investor_agent.py` (extend)

**Interfaces:**
- Consumes: `dossier.build_dossier`, `dossier.add_peer_relative` (Tasks 3-4); `data.fetch_info/fetch_ohlcv/fetch_estimates`, `sentiment.fetch_yf_news`; `_merge_pool` (existing).
- Produces: `_build_dossiers(pool, *, info_fn, ohlcv_fn, est_fn, news_fn, spy_ohlcv) -> dict[ticker]->dossier`; `_balanced_shortlist(results, pool, owned) -> list[str]` (deterministic, source-blind output — returns tickers only). `source_counts(picks_or_tickers, results) -> dict` helper for monitoring.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_investor_agent.py (add)
import quant.agent.investor as ia


def test_balanced_shortlist_not_starved_by_lopsided_sources():
    # value emits 15, canslim emits 4 → shortlist must still include canslim's top
    results = {
        "value": {"rows": [{"ticker": f"V{i}", "rank": i + 1, "score": 1.0} for i in range(15)]},
        "canslim": {"rows": [{"ticker": f"C{i}", "rank": i + 1, "score": 1.0} for i in range(4)]},
    }
    pool = ia._merge_pool(results)
    short = ia._balanced_shortlist(results, pool, owned=set())
    assert any(t.startswith("C") for t in short), "canslim source was starved"
    assert any(t.startswith("V") for t in short)
    assert len(short) <= 8


def test_build_dossiers_uses_injected_fetchers():
    pool = [{"ticker": "AAA", "strategies": ["value"], "best_rank": 1, "score": 1.0}]
    dossiers = ia._build_dossiers(
        pool,
        info_fn=lambda t: {"sector": "Tech", "currentPrice": 50.0, "trailingPE": 12.0},
        ohlcv_fn=lambda t: None, est_fn=lambda t: {"surprises": []},
        news_fn=lambda t: None, spy_ohlcv=None)
    assert dossiers["AAA"]["valuation"]["pe"] == 12.0
    assert "peer_relative" in dossiers["AAA"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_investor_agent.py -q -k "balanced or build_dossiers"`
Expected: FAIL (no attribute '_balanced_shortlist')

- [ ] **Step 3: Implement the orchestration helpers**

Add to `quant/agent/investor.py` (top-level):

```python
from concurrent.futures import ThreadPoolExecutor

import quant.agent.dossier as dossier


def _balanced_shortlist(results, pool, owned):
    """Deterministic, source-blind output: take each strategy's top-K (by its own
    rank), union + dedupe, cap at AGENT_SHORTLIST_N. Returns tickers only."""
    import quant.config as config
    seen, short = set(owned), []
    for name, payload in results.items():
        rows = sorted(payload.get("rows", []), key=lambda r: r.get("rank", 10**9))
        taken = 0
        for r in rows:
            t = r.get("ticker")
            if not t or t in seen:
                continue
            short.append(t); seen.add(t); taken += 1
            if taken >= config.AGENT_SHORTLIST_PER_SOURCE:
                break
    return short[:config.AGENT_SHORTLIST_N]


def _build_dossiers(pool, *, info_fn, ohlcv_fn, est_fn, news_fn, spy_ohlcv):
    import quant.config as config
    tickers = [e["ticker"] for e in pool]

    def _one(t):
        try:
            return t, dossier.build_dossier(
                t, info=info_fn(t), ohlcv=ohlcv_fn(t), spy_ohlcv=spy_ohlcv,
                news=news_fn(t), estimates=est_fn(t))
        except Exception as e:
            _log.warning("_build_dossiers: %s failed: %s", t, e)
            return t, None

    out = {}
    if tickers:
        with ThreadPoolExecutor(max_workers=config.AGENT_DOSSIER_WORKERS) as ex:
            for t, dos in ex.map(_one, tickers):
                if dos is not None:
                    out[t] = dos
    dossier.add_peer_relative(list(out.values()), min_group=config.AGENT_PEER_MIN_GROUP)
    return out


def source_counts(tickers, results):
    """Count how many of `tickers` came from each strategy (a ticker may count
    for multiple)."""
    by_src = {name: {r.get("ticker") for r in payload.get("rows", [])}
              for name, payload in results.items()}
    counts = {name: sum(1 for t in tickers if t in s) for name, s in by_src.items()}
    counts["other"] = sum(1 for t in tickers if not any(t in s for s in by_src.values()))
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_investor_agent.py -q -k "balanced or build_dossiers"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/agent/investor.py tests/test_investor_agent.py
git commit -m "feat(agent): concurrent dossier build + balanced blind shortlist"
```

---

### Task 7: the three LLM stages — analyst, critic, PM (prompts, parsers, fallbacks)

**Files:**
- Modify: `quant/agent/investor.py`
- Test: `tests/test_investor_agent.py`

**Interfaces:**
- Consumes: dossiers dict + shortlist (Task 6); `llm_fn(prompt)->str|None`.
- Produces: `_analyst(dossiers, shortlist, llm_fn) -> dict[ticker]->verdict`, `_critic(verdicts, dossiers, llm_fn) -> dict[ticker]->verdict`, `_pm(verdicts, llm_fn) -> list[ticker]`. A verdict is `{ticker, signal, confidence, thesis, risks, catalysts, bull, bear}`. Each has a deterministic fallback. A shared `_extract_json(text) -> dict|None` helper.

- [ ] **Step 1: Write the failing test**

```python
def _fake_llm(analyst_json=None, critic_json=None, pm_json=None):
    def f(prompt):
        if "STAGE=ANALYST" in prompt:
            return analyst_json
        if "STAGE=CRITIC" in prompt:
            return critic_json
        if "STAGE=PM" in prompt:
            return pm_json
        return None
    return f


def _dos(t, conf_price=10.0):
    return {"ticker": t, "sector": "Tech",
            "valuation": {"pe": 12.0, "ps": 2.0, "ev_ebitda": None},
            "growth": {"rev_growth": 0.2}, "quality": {"gross_margin": 0.5},
            "price_action": {"price": conf_price, "atr14": 1.0, "swing_low_20": conf_price*0.9,
                             "rsi14": 55.0, "pct_vs_200dma": 0.1},
            "peer_relative": {"pe_z": 0.5}, "analyst": {}, "estimates": {}, "news": None}


def test_analyst_parses_verdicts():
    dossiers = {"AAA": _dos("AAA")}
    j = '{"verdicts":[{"ticker":"AAA","signal":"bullish","confidence":80,"thesis":"cheap+growing","risks":"x","catalysts":"y","bull":"b","bear":"be"}]}'
    out = ia._analyst(dossiers, ["AAA"], _fake_llm(analyst_json=j))
    assert out["AAA"]["signal"] == "bullish" and out["AAA"]["confidence"] == 80


def test_analyst_fallback_on_llm_none():
    dossiers = {"AAA": _dos("AAA")}
    out = ia._analyst(dossiers, ["AAA"], _fake_llm(analyst_json=None))
    assert out["AAA"]["signal"] == "neutral"     # deterministic fallback


def test_pm_abstains_when_all_below_floor():
    verdicts = {"AAA": {"ticker": "AAA", "confidence": 20, "signal": "neutral"}}
    picks = ia._pm(verdicts, _fake_llm(pm_json=None))   # fallback path
    assert picks == []                                  # below AGENT_CONVICTION_FLOOR=50


def test_pm_caps_and_filters_by_floor_fallback():
    verdicts = {f"T{i}": {"ticker": f"T{i}", "confidence": 90 - i, "signal": "bullish"}
                for i in range(8)}
    picks = ia._pm(verdicts, _fake_llm(pm_json=None))
    assert len(picks) <= 5 and all(isinstance(t, str) for t in picks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_investor_agent.py -q -k "analyst or pm or critic"`
Expected: FAIL (no attribute '_analyst')

- [ ] **Step 3: Implement the three stages**

Add to `quant/agent/investor.py`:

```python
_GROUNDING = ("Use ONLY the numbers in each dossier; never invent a figure; "
              "null → 'unknown'. Reply with STRICT JSON only.")


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _analyst(dossiers, shortlist, llm_fn):
    rows = json.dumps([dossiers[t] for t in shortlist if t in dossiers], default=str)
    prompt = ("STAGE=ANALYST\nYou are a seasoned equity analyst. For EACH candidate, argue the "
              "bull case AND the bear case from the dossier, then give a verdict. " + _GROUNDING +
              ' Schema: {"verdicts":[{"ticker","signal":"bullish|neutral|bearish",'
              '"confidence":0-100,"thesis":"<=25w","risks","catalysts","bull","bear"}]}\n'
              "Reference: PE<20 cheap, rev_growth>15% strong, RSI>70 overbought, "
              "peer_relative z>+1 strong-vs-industry, target_upside>20% rich.\nDossiers:\n" + rows)
    data = _extract_json(llm_fn(prompt))
    out = {}
    if data and isinstance(data.get("verdicts"), list):
        for v in data["verdicts"]:
            t = v.get("ticker")
            if t in dossiers:
                out[t] = {"ticker": t, "signal": v.get("signal", "neutral"),
                          "confidence": int(v.get("confidence", 0) or 0),
                          "thesis": str(v.get("thesis", ""))[:200], "risks": str(v.get("risks", ""))[:200],
                          "catalysts": str(v.get("catalysts", ""))[:200],
                          "bull": str(v.get("bull", ""))[:200], "bear": str(v.get("bear", ""))[:200]}
    # deterministic fallback for any shortlisted name the LLM didn't return
    for t in shortlist:
        if t in dossiers and t not in out:
            out[t] = {"ticker": t, "signal": "neutral", "confidence": 0, "thesis": "no analyst verdict",
                      "risks": "", "catalysts": "", "bull": "", "bear": ""}
    return out


def _critic(verdicts, dossiers, llm_fn):
    payload = json.dumps({"verdicts": list(verdicts.values()),
                          "dossiers": {t: dossiers[t] for t in verdicts if t in dossiers}}, default=str)
    prompt = ("STAGE=CRITIC\nYou are a skeptical risk reviewer. For each verdict, strike any claim "
              "not supported by the dossier numbers and CAP confidence that the data does not justify. "
              + _GROUNDING + ' Return the SAME schema plus "critic_notes". Input:\n' + payload)
    data = _extract_json(llm_fn(prompt))
    if not data or not isinstance(data.get("verdicts"), list):
        return verdicts                      # fallback: pass analyst verdicts through
    out = dict(verdicts)
    for v in data["verdicts"]:
        t = v.get("ticker")
        if t in out:
            out[t] = {**out[t], "confidence": int(v.get("confidence", out[t]["confidence"]) or 0),
                      "signal": v.get("signal", out[t]["signal"]),
                      "critic_notes": str(v.get("critic_notes", ""))[:200]}
    return out


def _pm(verdicts, llm_fn):
    import quant.config as config
    floor, cap = config.AGENT_CONVICTION_FLOOR, config.AGENT_MAX_PICKS
    eligible = {t: v for t, v in verdicts.items() if v.get("confidence", 0) >= floor}
    prompt = ("STAGE=PM\nYou are the portfolio manager. From these analyst verdicts, choose the best "
              f"risk-adjusted set: buy ONLY names with confidence >= {floor}; return BETWEEN 0 and {cap} "
              "tickers; prefer cash to a weak buy. " + _GROUNDING +
              ' Schema: {"picks":[{"ticker","rationale":"<=15w"}]}\nVerdicts:\n'
              + json.dumps(list(verdicts.values()), default=str))
    data = _extract_json(llm_fn(prompt))
    picks = None
    if data and isinstance(data.get("picks"), list):
        chosen = [p.get("ticker") for p in data["picks"] if p.get("ticker") in eligible]
        picks = chosen[:cap]
    if picks is None:                        # fallback: floor-eligible by confidence
        picks = [t for t, _ in sorted(eligible.items(), key=lambda kv: -kv[1].get("confidence", 0))][:cap]
    return picks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_investor_agent.py -q -k "analyst or pm or critic"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add quant/agent/investor.py tests/test_investor_agent.py
git commit -m "feat(agent): analyst/critic/PM LLM stages with deterministic fallbacks"
```

---

### Task 8: wire `select_candidates` to the pipeline + levels + monitoring

**Files:**
- Modify: `quant/agent/investor.py` (`select_candidates`)
- Test: `tests/test_investor_agent.py`

**Interfaces:**
- Consumes: all of Tasks 6-7 + `dossier.suggested_levels`.
- Produces: `select_candidates(top_n=None, owned=None, llm_fn=None, *, fetchers=None) -> list` — picks enriched with `{ticker, rationale, signal, confidence, thesis, risks, catalysts, buy_low, buy_high, stop_loss, take_profit, strategies}`. `fetchers` is an optional dict of injected `{info_fn, ohlcv_fn, est_fn, news_fn, spy_ohlcv}` for tests; defaults to the real `data`/`sentiment` functions. Writes `.cache/agent_source_mix.csv` + `.cache/agent_reasons.log`.

- [ ] **Step 1: Write the failing test**

```python
def test_select_candidates_end_to_end_enriched(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.agent.investor.BUY_CANDIDATES_PATH", str(tmp_path / "bc.json"))
    monkeypatch.setattr("quant.agent.investor._SOURCE_MIX_PATH", str(tmp_path / "mix.csv"))
    monkeypatch.setattr("quant.agent.investor._REASONS_LOG_PATH", str(tmp_path / "reasons.log"))
    results = {"value": {"rows": [{"ticker": "AAA", "rank": 1, "score": 1.0}]},
               "canslim": {"rows": [{"ticker": "BBB", "rank": 1, "score": 1.0}]}}
    monkeypatch.setattr("quant.strategies.contract.load_strategy_results", lambda: results)
    fetchers = {"info_fn": lambda t: {"sector": "Tech", "currentPrice": 50.0, "trailingPE": 12.0},
                "ohlcv_fn": lambda t: None, "est_fn": lambda t: {"surprises": []},
                "news_fn": lambda t: None, "spy_ohlcv": None}
    analyst = '{"verdicts":[{"ticker":"AAA","signal":"bullish","confidence":80,"thesis":"good","risks":"r","catalysts":"c","bull":"b","bear":"be"},{"ticker":"BBB","signal":"bullish","confidence":75,"thesis":"ok","risks":"r","catalysts":"c","bull":"b","bear":"be"}]}'
    pm = '{"picks":[{"ticker":"AAA","rationale":"top"},{"ticker":"BBB","rationale":"two"}]}'
    llm = _fake_llm(analyst_json=analyst, critic_json=None, pm_json=pm)
    picks = ia.select_candidates(owned=set(), llm_fn=llm, fetchers=fetchers)
    tickers = {p["ticker"] for p in picks}
    assert tickers == {"AAA", "BBB"}
    p = picks[0]
    assert set(p) >= {"ticker", "signal", "confidence", "thesis", "buy_low", "buy_high", "stop_loss", "take_profit", "strategies"}
    assert (tmp_path / "mix.csv").exists()
    assert (tmp_path / "reasons.log").read_text().strip() != ""


def test_select_candidates_all_llm_fail_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.agent.investor.BUY_CANDIDATES_PATH", str(tmp_path / "bc.json"))
    monkeypatch.setattr("quant.agent.investor._SOURCE_MIX_PATH", str(tmp_path / "mix.csv"))
    monkeypatch.setattr("quant.agent.investor._REASONS_LOG_PATH", str(tmp_path / "reasons.log"))
    results = {"value": {"rows": [{"ticker": "AAA", "rank": 1, "score": 1.0}]}}
    monkeypatch.setattr("quant.strategies.contract.load_strategy_results", lambda: results)
    fetchers = {"info_fn": lambda t: {"currentPrice": 50.0}, "ohlcv_fn": lambda t: None,
                "est_fn": lambda t: {"surprises": []}, "news_fn": lambda t: None, "spy_ohlcv": None}
    picks = ia.select_candidates(owned=set(), llm_fn=lambda p: None, fetchers=fetchers)
    # confidence 0 < floor → abstains; still writes a (possibly empty) file without raising
    assert isinstance(picks, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_investor_agent.py -q -k "end_to_end or all_llm_fail"`
Expected: FAIL (select_candidates has no `fetchers` kwarg / no level fields)

- [ ] **Step 3: Rewrite `select_candidates`**

Replace the body of `select_candidates` and add the module-level paths + a default-fetchers helper:

```python
_SOURCE_MIX_PATH = os.path.join(paths.REPO_ROOT, ".cache", "agent_source_mix.csv")
_REASONS_LOG_PATH = os.path.join(paths.REPO_ROOT, ".cache", "agent_reasons.log")


def _default_fetchers():
    import quant.data.market as data
    import quant.signals.sentiment as sentiment
    import quant.config as config
    spy = None
    try:
        spy = data.fetch_ohlcv("SPY", period="1y")
    except Exception:
        spy = None
    news_fn = (lambda t: data and sentiment.fetch_yf_news([t])) if config.AGENT_INCLUDE_NEWS else (lambda t: None)
    return {"info_fn": data.fetch_info, "ohlcv_fn": lambda t: data.fetch_ohlcv(t, period="1y"),
            "est_fn": data.fetch_estimates, "news_fn": news_fn, "spy_ohlcv": spy}


def _log_monitoring(results, shortlist, picks):
    import datetime as _dt
    day = _dt.date.today().isoformat()
    pt = [p["ticker"] for p in picks]
    sc_pick, sc_short = source_counts(pt, results), source_counts(shortlist, results)
    try:
        os.makedirs(os.path.dirname(_SOURCE_MIX_PATH), exist_ok=True)
        new = not os.path.exists(_SOURCE_MIX_PATH)
        with open(_SOURCE_MIX_PATH, "a") as f:
            if new:
                f.write("date,n_picks,n_value,n_canslim,n_other,shortlist_value,shortlist_canslim\n")
            f.write(f"{day},{len(pt)},{sc_pick.get('value',0)},{sc_pick.get('canslim',0)},"
                    f"{sc_pick.get('other',0)},{sc_short.get('value',0)},{sc_short.get('canslim',0)}\n")
        with open(_REASONS_LOG_PATH, "a") as f:
            for p in picks:
                f.write(f"{day} {p['ticker']} {p.get('signal','?')} conf={p.get('confidence','?')} "
                        f"buy={p.get('buy_low')}-{p.get('buy_high')} stop={p.get('stop_loss')} "
                        f"tp={p.get('take_profit')} | {p.get('thesis','')}; risks: {p.get('risks','')}\n")
    except Exception as e:
        _log.warning("_log_monitoring: %s", e)


def select_candidates(top_n=None, owned=None, llm_fn=None, *, fetchers=None) -> list:
    """Dossier-grounded pipeline: balanced blind shortlist → analyst → critic → PM
    (0..AGENT_MAX_PICKS, conviction floor) → enriched picks + advisory levels.
    Fail-open: any stage failure degrades to a deterministic fallback."""
    import quant.config as config
    import quant.strategies.contract as strategies
    llm_fn = llm_fn or _default_llm
    if owned is None:
        try:
            import quant.execution.orders as orders
            owned = {p["symbol"] for p in orders._load_portfolio_cache().get("positions", [])}
        except Exception:
            owned = set()

    results = strategies.load_strategy_results()
    pool = [e for e in _merge_pool(results) if e["ticker"] not in owned]
    by_ticker = {e["ticker"]: e for e in pool}

    picks = []
    shortlist = []
    if pool:
        fetchers = fetchers or _default_fetchers()
        dossiers = _build_dossiers(pool, **fetchers)
        shortlist = [t for t in _balanced_shortlist(results, pool, owned) if t in dossiers]
        if shortlist:
            verdicts = _critic(_analyst(dossiers, shortlist, llm_fn), dossiers, llm_fn)
            chosen = _pm(verdicts, llm_fn)
            for t in chosen:
                v = verdicts.get(t, {})
                lv = dossier.suggested_levels(dossiers[t], buy_band_atr=config.AGENT_BUY_BAND_ATR,
                                              stop_atr_mult=config.AGENT_STOP_ATR_MULT,
                                              target_r=config.AGENT_TARGET_R)
                picks.append({"ticker": t, "rationale": v.get("thesis", ""), "signal": v.get("signal"),
                              "confidence": v.get("confidence"), "thesis": v.get("thesis", ""),
                              "risks": v.get("risks", ""), "catalysts": v.get("catalysts", ""),
                              **lv, "strategies": by_ticker[t]["strategies"]})

    _log_monitoring(results, shortlist, picks)
    os.makedirs(os.path.dirname(BUY_CANDIDATES_PATH), exist_ok=True)
    tmp = BUY_CANDIDATES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "picks": picks}, f)
    os.replace(tmp, BUY_CANDIDATES_PATH)
    return picks
```

Delete the now-unused `_build_prompt` and `_parse_llm` (superseded by the staged pipeline). Keep `_merge_pool`, `_default_llm`, `run_investor_review` (separate feature).

- [ ] **Step 4: Run the investor-agent suite**

Run: `python3 -m pytest tests/test_investor_agent.py -q`
Expected: PASS (new + existing select tests). If an existing test asserted the old `_build_prompt`/`_parse_llm`, update it to the staged pipeline (the contract is the enriched picks list; `ticker` is still present).

- [ ] **Step 5: Commit**

```bash
git add quant/agent/investor.py tests/test_investor_agent.py
git commit -m "feat(agent): wire dossier→analyst→critic→PM pipeline + levels + monitoring"
```

---

### Task 9: full suite + docs + dry-run

**Files:**
- Modify: `README.md` (agent section), `docs/system_overview.html` (investor card), `docs/architecture.html` (`SUB.agent` detail flow)
- Verify: full test suite

- [ ] **Step 1: Full suite green**

Run: `python3 -m pytest -p no:cacheprovider 2>&1 | tail -2`
Expected: all pass (existing count + the new agent tests), 0 failures.

- [ ] **Step 2: Offline dry-run (lopsided sources, stage-aware fake LLM)**

```bash
cd /Users/zl/works/stock
python3 - <<'PY'
import quant.agent.investor as ia
results = {"value": {"rows": [{"ticker": f"V{i}", "rank": i+1, "score": 1.0} for i in range(15)]},
           "canslim": {"rows": [{"ticker": "C0", "rank": 1, "score": 1.0}]}}
import quant.strategies.contract as sc
sc.load_strategy_results = lambda: results
fetchers = {"info_fn": lambda t: {"sector":"Tech","currentPrice":50.0,"trailingPE":12.0,"targetMeanPrice":60.0},
            "ohlcv_fn": lambda t: None, "est_fn": lambda t: {"surprises":[]},
            "news_fn": lambda t: None, "spy_ohlcv": None}
def llm(p):
    if "STAGE=ANALYST" in p:
        import re, json
        ts = re.findall(r'"ticker": "([^"]+)"', p)
        return json.dumps({"verdicts":[{"ticker":t,"signal":"bullish","confidence":70,"thesis":"ok","risks":"r","catalysts":"c","bull":"b","bear":"be"} for t in dict.fromkeys(ts)]})
    if "STAGE=PM" in p:
        return '{"picks":[{"ticker":"V0","rationale":"x"},{"ticker":"C0","rationale":"y"}]}'
    return None
picks = ia.select_candidates(owned=set(), llm_fn=llm, fetchers=fetchers)
print("PICKS:", [(p["ticker"], p["confidence"], p["buy_low"], p["stop_loss"], p["take_profit"]) for p in picks])
PY
```
Expected: prints picks including both a `V*` and `C0` (balance held), each with numeric or None levels — no exception.

- [ ] **Step 3: Update docs**

- `README.md` agent section: describe the dossier → balanced-shortlist → analyst → critic → PM pipeline, the `0–AGENT_MAX_PICKS` abstention, advisory buy/stop/tp levels, and `.cache/agent_source_mix.csv` + `.cache/agent_reasons.log`.
- `docs/system_overview.html`: update the `investor_agent`/`quant/agent/investor.py` card body to the new pipeline.
- `docs/architecture.html`: replace the `SUB.agent` detail-flow nodes with: `Pool` → `Dossiers (peer-relative)` → `Balanced blind shortlist` → `Analyst (bull/bear)` → `Critic` → `PM (0–5, floor)` → `Picks + levels` → `buy_candidates.json`.

Verify HTML parses:
```bash
python3 -c "import html.parser; [html.parser.HTMLParser().feed(open('docs/'+f).read()) for f in ('system_overview.html','architecture.html')]; print('HTML OK')"
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/system_overview.html docs/architecture.html
git commit -m "docs: investor-agent due-diligence pipeline (dossier→analyst→critic→PM)"
```

---

## Self-Review

**Spec coverage:** config (T1); pure dossier + peer-relative + levels (T2-T4); estimates fetch (T5); concurrent dossier build + balanced blind shortlist + source monitoring helper (T6); analyst/critic/PM stages + fallbacks + abstention (T7); wiring + levels attach + monitoring + reasons log + back-compat picks (T8); suite + docs + dry-run (T9). De-bias (balanced/blind/monitor), grounding, fail-open, advisory levels, reason log — all mapped. ✓

**Placeholder scan:** every code step contains full code; commands have expected output; no TBD/TODO. ✓

**Type consistency:** `build_dossier`/`add_peer_relative`/`suggested_levels`/`compact_line` signatures match across T2-T4 and their callers in T6/T8; verdict dict shape `{ticker,signal,confidence,thesis,risks,catalysts,bull,bear}` is consistent across `_analyst`/`_critic`/`_pm`/`select_candidates`; `fetchers` dict keys `{info_fn,ohlcv_fn,est_fn,news_fn,spy_ohlcv}` match between `_build_dossiers`, `_default_fetchers`, and the tests; level keys `{buy_low,buy_high,stop_loss,take_profit}` consistent. ✓

**Note:** `select_candidates` now uses `AGENT_MAX_PICKS`/`AGENT_CONVICTION_FLOOR` (not `ENSEMBLE_TOP_N`); the legacy `top_n` arg is accepted but unused (kept for signature back-compat). The watchdog's daily ensemble call passes no pick-count, so this is safe.
