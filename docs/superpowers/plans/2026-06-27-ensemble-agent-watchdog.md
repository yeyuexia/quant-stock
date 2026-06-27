# Multi-strategy Ensemble → Agent Pick → Watchdog Auto-buy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run isolated stock-selection strategies, have one Claude agent pick the top 4 across their results, and feed those 4 to the existing watchdog buy path (which auto-executes the buy).

**Architecture:** A `strategies.py` contract lets each strategy write its own `.cache/strategies/<name>.json`; `value_screen.py` (new value+quality screen) and a thin CANSLIM adapter are the two registered strategies; `investor_agent.select_candidates()` merges/dedupes/rule-filters and asks the `claude` CLI for the top 4 (rule-ranked fallback on failure) → `.cache/buy_candidates.json`; `watchdog._get_screened_stocks()` reads that file. The buy mechanism downstream is unchanged.

**Tech Stack:** Python, pytest, pandas, yfinance (via `data.py`), the local `claude` CLI (via `subprocess`).

## Global Constraints

- Python; tests via `pytest`. Run one test: `python3 -m pytest tests/<file>::<name> -v`.
- Tests are state-isolated by the autouse `_isolate_persistent_state` fixture in `tests/conftest.py`. Use `tmp_path`/`monkeypatch`; never write real `.cache` state in tests.
- Fail-open everywhere (mirror `screener._fundamental_ok`): missing data → skip that factor/ticker; a throwing strategy/agent never aborts the run.
- Config values (verbatim): `VS_MIN_DOLLAR_VOLUME = 2_000_000`, `VS_MIN_PRICE = 5.0`, `VS_MIN_MARKET_CAP = 300_000_000`, `VS_TOP_N = 20`, `VS_WEIGHTS = {"value": 0.5, "quality": 0.35, "improving": 0.15}`, `ENSEMBLE_TOP_N = 4`, `ENSEMBLE_STRATEGIES = ["value", "canslim"]`.
- Strategy result schema (verbatim): `{"strategy": name, "generated_at": iso, "rows": [{"ticker": str, "score": float, "rank": int, "factors": dict}]}`.
- Buy-candidates schema (verbatim): `{"generated_at": iso, "picks": [{"ticker": str, "rationale": str, "strategies": [str]}]}`.
- Data access: `data.fetch_info(ticker) -> dict` (yfinance `.info`, cached, `{}` on failure); `data.fetch_ohlcv` / `data.fetch_prices` for price/volume; `data.fetch_fundamentals(ticker) -> dict` (`eps_q_growth`, `revenue_growth`).
- Per standing user instruction: update `README.md` before reporting done (Task 7).
- Commit after every task.

---

### Task 1: Config additions

**Files:**
- Modify: `config.py` (append after the existing screener config block)
- Test: `tests/test_config_flags.py`

**Interfaces:**
- Produces: the constants listed in Global Constraints.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_flags.py`:

```python
def test_ensemble_config_defaults():
    import config
    assert config.VS_MIN_DOLLAR_VOLUME == 2_000_000
    assert config.VS_MIN_PRICE == 5.0
    assert config.VS_MIN_MARKET_CAP == 300_000_000
    assert config.VS_TOP_N == 20
    assert config.VS_WEIGHTS == {"value": 0.5, "quality": 0.35, "improving": 0.15}
    assert config.ENSEMBLE_TOP_N == 4
    assert config.ENSEMBLE_STRATEGIES == ["value", "canslim"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_flags.py::test_ensemble_config_defaults -v`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'VS_MIN_DOLLAR_VOLUME'`

- [ ] **Step 3: Add the constants**

Append near the end of `config.py` (after the watchlist block, before the strategy-overrides import section):

```python
# ── Value+Quality screen + ensemble ─────────────────────────────
VS_MIN_DOLLAR_VOLUME = 2_000_000     # ADV * price liquidity gate
VS_MIN_PRICE = 5.0                   # no penny stocks
VS_MIN_MARKET_CAP = 300_000_000      # no micro-caps
VS_TOP_N = 20                        # value_screen emits this many
VS_WEIGHTS = {"value": 0.5, "quality": 0.35, "improving": 0.15}
ENSEMBLE_TOP_N = 4                   # agent's final buy candidates
ENSEMBLE_STRATEGIES = ["value", "canslim"]   # registered strategy names
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_flags.py::test_ensemble_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config_flags.py
git commit -m "feat(config): value-screen + ensemble constants"
```

---

### Task 2: Strategy result contract + runner (`strategies.py`)

**Files:**
- Create: `strategies.py`
- Test: `tests/test_strategies.py`

**Interfaces:**
- Consumes: `config.ENSEMBLE_STRATEGIES`.
- Produces:
  - `STRATEGIES_DIR` (str path `.cache/strategies`)
  - `write_strategy_result(name: str, rows: list[dict]) -> str` — writes `.cache/strategies/<name>.json` per the schema, returns the path.
  - `load_strategy_results() -> dict[str, dict]` — `{name: parsed_json}`, fail-open per file (skip missing/corrupt).
  - `run_strategies(registry: dict[str, callable]) -> list[str]` — calls each registered `name -> callable` that returns `rows`; writes each via `write_strategy_result`; a callable that raises is logged and skipped; returns written paths.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_strategies.py`:

```python
import json
import strategies


def test_write_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    rows = [{"ticker": "AAPL", "score": 1.2, "rank": 1, "factors": {"v": 0.3}}]
    path = strategies.write_strategy_result("value", rows)
    assert path.endswith("value.json")
    loaded = strategies.load_strategy_results()
    assert loaded["value"]["strategy"] == "value"
    assert loaded["value"]["rows"][0]["ticker"] == "AAPL"
    assert "generated_at" in loaded["value"]


def test_load_skips_corrupt_file(tmp_path, monkeypatch):
    d = tmp_path / "strat"
    d.mkdir()
    (d / "value.json").write_text('{"strategy": "value", "rows": []}')
    (d / "canslim.json").write_text("{ broken json")
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(d))
    loaded = strategies.load_strategy_results()
    assert "value" in loaded
    assert "canslim" not in loaded   # corrupt file skipped, no crash


def test_run_strategies_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    def good():
        return [{"ticker": "MSFT", "score": 2.0, "rank": 1, "factors": {}}]
    def bad():
        raise RuntimeError("boom")
    paths = strategies.run_strategies({"value": good, "canslim": bad})
    assert any(p.endswith("value.json") for p in paths)
    loaded = strategies.load_strategy_results()
    assert "value" in loaded and "canslim" not in loaded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_strategies.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies'`

- [ ] **Step 3: Implement `strategies.py`**

Create `strategies.py`:

```python
"""Isolated-strategy contract: each strategy writes its own result file under
.cache/strategies/, and the agent reads them all. One strategy failing never
affects the others.
"""
import datetime as dt
import json
import logging
import os

_log = logging.getLogger(__name__)

STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), ".cache", "strategies")


def write_strategy_result(name: str, rows: list) -> str:
    """Persist one strategy's rows to .cache/strategies/<name>.json."""
    os.makedirs(STRATEGIES_DIR, exist_ok=True)
    path = os.path.join(STRATEGIES_DIR, f"{name}.json")
    payload = {
        "strategy": name,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rows": rows,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return path


def load_strategy_results() -> dict:
    """Return {name: parsed_json} for every readable file. Fail-open per file."""
    out: dict = {}
    if not os.path.isdir(STRATEGIES_DIR):
        return out
    for fname in os.listdir(STRATEGIES_DIR):
        if not fname.endswith(".json"):
            continue
        name = fname[:-len(".json")]
        try:
            with open(os.path.join(STRATEGIES_DIR, fname)) as f:
                out[name] = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("load_strategy_results: skipping %s: %s", fname, e)
    return out


def run_strategies(registry: dict) -> list:
    """Run each registered name->callable() that returns rows; write each.
    A callable that raises is logged and skipped (isolation)."""
    paths = []
    for name, fn in registry.items():
        try:
            rows = fn()
        except Exception as e:
            _log.warning("run_strategies: strategy %s failed: %s", name, e)
            continue
        paths.append(write_strategy_result(name, rows or []))
    return paths
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_strategies.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add strategies.py tests/test_strategies.py
git commit -m "feat(strategies): isolated-strategy result contract + runner"
```

---

### Task 3: Value+Quality screen (`value_screen.py`)

**Files:**
- Create: `value_screen.py`
- Test: `tests/test_value_screen.py`

**Interfaces:**
- Consumes: `config.VS_*`, `data.fetch_info`, `data.fetch_fundamentals`, `data.fetch_prices`, `strategies.write_strategy_result`.
- Produces:
  - `screen_value_quality(tickers: list[str], info_fn=..., price_fn=..., fund_fn=...) -> list[dict]` — returns ranked rows `{"ticker","score","rank","factors"}`. The `*_fn` params default to the `data.*` functions and exist for test injection.
  - `run(tickers=None) -> list[dict]` — calls `screen_value_quality` over `tickers or config.WATCHLIST`, writes via `strategies.write_strategy_result("value", rows)`, returns rows.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_value_screen.py`:

```python
import value_screen


def _info(price, mcap, advol, fcf, roe, d2e, fpe, p2b, ev_ebitda):
    return {
        "currentPrice": price, "marketCap": mcap, "averageVolume": advol,
        "freeCashflow": fcf, "returnOnEquity": roe, "debtToEquity": d2e,
        "forwardPE": fpe, "priceToBook": p2b, "enterpriseToEbitda": ev_ebitda,
    }


def _make_info_fn(table):
    return lambda t: table.get(t, {})


def test_gates_exclude_illiquid_cheap_microcap(monkeypatch):
    table = {
        # below price floor ($5)
        "PENNY": _info(3.0, 1e9, 1e6, 1e8, 0.2, 50, 12, 3, 10),
        # below dollar-volume gate (price*advol = 10*100 = 1000)
        "ILLQ": _info(10.0, 1e9, 100, 1e8, 0.2, 50, 12, 3, 10),
        # below market cap floor
        "MICRO": _info(10.0, 1e8, 1e6, 1e8, 0.2, 50, 12, 3, 10),
        # negative FCF AND negative ROE (trap)
        "JUNK": _info(10.0, 1e9, 1e6, -1e8, -0.2, 50, 12, 3, 10),
        # clean
        "GOOD": _info(50.0, 5e9, 1e6, 5e8, 0.25, 30, 10, 2, 8),
    }
    rows = value_screen.screen_value_quality(
        list(table), info_fn=_make_info_fn(table),
        fund_fn=lambda t: {}, price_fn=lambda t: None)
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["GOOD"]   # only the clean one survives the gates


def test_cheaper_higher_quality_ranks_first(monkeypatch):
    # Two survivors; CHEAP has higher FCF yield + ROE → higher composite.
    table = {
        "CHEAP": _info(20.0, 1e9, 1e6, 2e8, 0.30, 10, 8, 1.0, 6),
        "RICH":  _info(20.0, 1e9, 1e6, 2e7, 0.05, 200, 40, 6.0, 30),
    }
    rows = value_screen.screen_value_quality(
        ["CHEAP", "RICH"], info_fn=_make_info_fn(table),
        fund_fn=lambda t: {}, price_fn=lambda t: None)
    assert [r["ticker"] for r in rows] == ["CHEAP", "RICH"]
    assert rows[0]["rank"] == 1
    assert rows[0]["score"] > rows[1]["score"]


def test_fail_open_on_empty_info(monkeypatch):
    rows = value_screen.screen_value_quality(
        ["X"], info_fn=lambda t: {}, fund_fn=lambda t: {}, price_fn=lambda t: None)
    assert rows == []   # no data → excluded, no crash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_value_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'value_screen'`

- [ ] **Step 3: Implement `value_screen.py`**

Create `value_screen.py`:

```python
"""Value+Quality screen — one ensemble strategy. Ranks a universe by a
cross-sectional z-score of value + quality (+ optional improving) factors,
after hard liquidity/quality gates. Fail-open on missing data.
"""
import argparse
import logging
import math

import config
import strategies

_log = logging.getLogger(__name__)


def _price_of(info: dict) -> float:
    for k in ("currentPrice", "regularMarketPrice", "previousClose"):
        v = info.get(k)
        if isinstance(v, (int, float)) and v == v and v > 0:
            return float(v)
    return 0.0


def _finite(v):
    return isinstance(v, (int, float)) and v == v and abs(v) != float("inf")


def _passes_gates(info: dict) -> bool:
    price = _price_of(info)
    if price < config.VS_MIN_PRICE:
        return False
    advol = info.get("averageVolume")
    if not _finite(advol) or price * float(advol) < config.VS_MIN_DOLLAR_VOLUME:
        return False
    mcap = info.get("marketCap")
    if not _finite(mcap) or float(mcap) < config.VS_MIN_MARKET_CAP:
        return False
    fcf = info.get("freeCashflow")
    roe = info.get("returnOnEquity")
    fcf_pos = _finite(fcf) and fcf > 0
    roe_pos = _finite(roe) and roe > 0
    if not (fcf_pos or roe_pos):   # trap guard
        return False
    return True


def _raw_factors(info: dict, fund: dict) -> dict:
    """Each factor: higher = more attractive. Absent inputs → key omitted."""
    f = {}
    mcap = info.get("marketCap")
    fcf = info.get("freeCashflow")
    if _finite(fcf) and _finite(mcap) and mcap > 0:
        f["fcf_yield"] = float(fcf) / float(mcap)
    fpe = info.get("forwardPE")
    if _finite(fpe) and fpe > 0:
        f["earnings_yield"] = 1.0 / float(fpe)
    ev = info.get("enterpriseToEbitda")
    if _finite(ev) and ev > 0:
        f["ev_ebitda_inv"] = 1.0 / float(ev)
    p2b = info.get("priceToBook")
    if _finite(p2b) and p2b > 0:
        f["book_market"] = 1.0 / float(p2b)
    roe = info.get("returnOnEquity")
    if _finite(roe):
        f["roe"] = float(roe)
    d2e = info.get("debtToEquity")
    if _finite(d2e) and d2e >= 0:
        f["inv_debt"] = 1.0 / (1.0 + float(d2e))
    if _finite(fund.get("eps_q_growth")):
        f["eps_q_growth"] = float(fund["eps_q_growth"])
    if _finite(fund.get("revenue_growth")):
        f["revenue_growth"] = float(fund["revenue_growth"])
    return f


_GROUPS = {
    "value": ("fcf_yield", "earnings_yield", "ev_ebitda_inv", "book_market"),
    "quality": ("roe", "inv_debt"),
    "improving": ("eps_q_growth", "revenue_growth"),
}


def _zscores(values: list) -> list:
    """Winsorized (±3σ) z-scores; all-equal/empty → zeros."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return [0.0 for _ in values]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var)
    if sd == 0:
        return [0.0 for _ in values]
    out = []
    for v in values:
        if v is None:
            out.append(0.0)
        else:
            z = (v - mean) / sd
            out.append(max(-3.0, min(3.0, z)))
    return out


def screen_value_quality(tickers, *, info_fn=None, fund_fn=None, price_fn=None):
    """Return ranked rows [{ticker, score, rank, factors}] (best first)."""
    from data import fetch_info, fetch_fundamentals
    info_fn = info_fn or fetch_info
    fund_fn = fund_fn or fetch_fundamentals

    survivors = []
    for t in tickers:
        try:
            info = info_fn(t) or {}
        except Exception as e:
            _log.warning("value_screen: fetch_info(%s) failed: %s", t, e)
            continue
        if not info or not _passes_gates(info):
            continue
        try:
            fund = fund_fn(t) or {}
        except Exception:
            fund = {}
        survivors.append({"ticker": t, "raw": _raw_factors(info, fund),
                          "price": _price_of(info)})

    if not survivors:
        return []

    # Cross-sectional z per raw factor.
    all_keys = set().union(*[s["raw"].keys() for s in survivors])
    zmap = {}
    for key in all_keys:
        col = [s["raw"].get(key) for s in survivors]
        for s, z in zip(survivors, _zscores(col)):
            zmap.setdefault(id(s), {})[key] = z

    rows = []
    for s in survivors:
        zs = zmap[id(s)]
        group_z = {}
        for g, keys in _GROUPS.items():
            present = [zs[k] for k in keys if k in zs]
            group_z[g] = sum(present) / len(present) if present else 0.0
        score = sum(config.VS_WEIGHTS[g] * group_z[g] for g in config.VS_WEIGHTS)
        rows.append({
            "ticker": s["ticker"], "score": round(score, 4),
            "factors": {**{k: round(v, 4) for k, v in s["raw"].items()},
                        "value_z": round(group_z["value"], 4),
                        "quality_z": round(group_z["quality"], 4),
                        "improving_z": round(group_z["improving"], 4),
                        "price": s["price"]},
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    rows = rows[:config.VS_TOP_N]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def run(tickers=None):
    rows = screen_value_quality(list(tickers or config.WATCHLIST))
    strategies.write_strategy_result("value", rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Value+Quality screen")
    ap.add_argument("--tickers", default=None,
                    help="comma-separated; defaults to config.WATCHLIST")
    args = ap.parse_args()
    tickers = args.tickers.split(",") if args.tickers else None
    rows = run(tickers)
    for r in rows:
        print(f"{r['rank']:>2}  {r['ticker']:<6}  score={r['score']:+.3f}  "
              f"V={r['factors']['value_z']:+.2f} Q={r['factors']['quality_z']:+.2f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_value_screen.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add value_screen.py tests/test_value_screen.py
git commit -m "feat(value_screen): value+quality screen strategy"
```

---

### Task 4: CANSLIM adapter + strategy registry

**Files:**
- Modify: `strategies.py` (add `default_registry()` + canslim adapter)
- Test: `tests/test_strategies.py` (add a test)

**Interfaces:**
- Consumes: `screener.screen_stocks()` (returns a pandas DataFrame with at least columns `ticker` and `composite`, already sorted best-first), `value_screen.run`.
- Produces: `strategies.default_registry() -> dict[str, callable]` mapping each name in `config.ENSEMBLE_STRATEGIES` to a zero-arg callable returning rows.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_strategies.py`:

```python
def test_canslim_adapter_maps_dataframe_rows(monkeypatch):
    import pandas as pd
    import strategies as S
    df = pd.DataFrame([
        {"ticker": "AAA", "composite": 9.0, "rs": 95},
        {"ticker": "BBB", "composite": 7.0, "rs": 80},
    ])
    monkeypatch.setattr("screener.screen_stocks", lambda: df)
    rows = S._canslim_rows()
    assert rows[0] == {"ticker": "AAA", "score": 9.0, "rank": 1,
                       "factors": {"composite": 9.0, "rs": 95}}
    assert rows[1]["rank"] == 2


def test_default_registry_has_configured_strategies():
    import strategies as S
    reg = S.default_registry()
    assert set(reg) == {"value", "canslim"}
    assert all(callable(fn) for fn in reg.values())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_strategies.py -k "canslim or default_registry" -v`
Expected: FAIL — `AttributeError: module 'strategies' has no attribute '_canslim_rows'`

- [ ] **Step 3: Implement the adapter + registry**

Append to `strategies.py`:

```python
def _canslim_rows() -> list:
    """Adapt screener.screen_stocks() DataFrame to strategy rows."""
    from screener import screen_stocks
    df = screen_stocks()
    if df is None or df.empty:
        return []
    rows = []
    for i, (_, r) in enumerate(df.iterrows(), 1):
        d = r.to_dict()
        ticker = str(d.pop("ticker"))
        score = float(d.get("composite", 0.0) or 0.0)
        factors = {k: (float(v) if isinstance(v, (int, float)) else v)
                   for k, v in d.items()}
        rows.append({"ticker": ticker, "score": score, "rank": i,
                     "factors": factors})
    return rows


def default_registry() -> dict:
    """Map each config.ENSEMBLE_STRATEGIES name to a zero-arg rows-producer."""
    import config
    import value_screen
    available = {
        "value": value_screen.run,
        "canslim": _canslim_rows,
    }
    return {name: available[name] for name in config.ENSEMBLE_STRATEGIES
            if name in available}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_strategies.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add strategies.py tests/test_strategies.py
git commit -m "feat(strategies): CANSLIM adapter + default registry"
```

---

### Task 5: Agent selection (`investor_agent.select_candidates`)

**Files:**
- Modify: `investor_agent.py` (add `select_candidates` + helpers)
- Test: `tests/test_investor_agent.py`

**Interfaces:**
- Consumes: `strategies.load_strategy_results`, `config.ENSEMBLE_TOP_N`, the `claude` CLI via `subprocess` (reuse the module's existing pattern), `orders._load_portfolio_cache` for owned tickers.
- Produces:
  - `BUY_CANDIDATES_PATH` (str path `.cache/buy_candidates.json`)
  - `select_candidates(top_n=None, owned=None, llm_fn=None) -> list[dict]` — returns and persists picks `[{ticker, rationale, strategies}]`. `llm_fn(prompt)->str|None` is injectable for tests (defaults to the real claude CLI call). On any LLM failure/invalid output, falls back to rule-ranked top-N.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_investor_agent.py`:

```python
import json
import investor_agent


def _seed_strategies(tmp_path, monkeypatch):
    import strategies
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    strategies.write_strategy_result("value", [
        {"ticker": "AAA", "score": 2.0, "rank": 1, "factors": {}},
        {"ticker": "BBB", "score": 1.0, "rank": 2, "factors": {}},
    ])
    strategies.write_strategy_result("canslim", [
        {"ticker": "AAA", "score": 9.0, "rank": 1, "factors": {}},
        {"ticker": "CCC", "score": 8.0, "rank": 2, "factors": {}},
    ])
    monkeypatch.setattr(investor_agent, "BUY_CANDIDATES_PATH",
                        str(tmp_path / "buy_candidates.json"))


def test_select_falls_back_to_rules_when_llm_unavailable(tmp_path, monkeypatch):
    _seed_strategies(tmp_path, monkeypatch)
    picks = investor_agent.select_candidates(
        top_n=2, owned=set(), llm_fn=lambda prompt: None)  # LLM "fails"
    tickers = [p["ticker"] for p in picks]
    assert len(picks) == 2
    assert "AAA" in tickers          # consensus name (in both lists) ranks first
    assert picks[0]["ticker"] == "AAA"
    assert set(picks[0]["strategies"]) == {"value", "canslim"}
    # persisted
    saved = json.loads(open(investor_agent.BUY_CANDIDATES_PATH).read())
    assert len(saved["picks"]) == 2


def test_select_excludes_owned(tmp_path, monkeypatch):
    _seed_strategies(tmp_path, monkeypatch)
    picks = investor_agent.select_candidates(
        top_n=4, owned={"AAA"}, llm_fn=lambda prompt: None)
    assert "AAA" not in [p["ticker"] for p in picks]


def test_select_uses_valid_llm_output(tmp_path, monkeypatch):
    _seed_strategies(tmp_path, monkeypatch)
    def fake_llm(prompt):
        return json.dumps({"picks": [
            {"ticker": "CCC", "rationale": "cheap turnaround"},
            {"ticker": "BBB", "rationale": "quality compounder"},
        ]})
    picks = investor_agent.select_candidates(top_n=2, owned=set(), llm_fn=fake_llm)
    assert [p["ticker"] for p in picks] == ["CCC", "BBB"]
    assert picks[0]["rationale"] == "cheap turnaround"


def test_select_rejects_hallucinated_ticker_and_falls_back(tmp_path, monkeypatch):
    _seed_strategies(tmp_path, monkeypatch)
    picks = investor_agent.select_candidates(
        top_n=2, owned=set(),
        llm_fn=lambda prompt: json.dumps({"picks": [{"ticker": "ZZZ", "rationale": "x"}]}))
    # ZZZ not in the pool → invalid → rule fallback
    assert [p["ticker"] for p in picks][0] == "AAA"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_investor_agent.py -k select -v`
Expected: FAIL — `AttributeError: module 'investor_agent' has no attribute 'select_candidates'`

- [ ] **Step 3: Implement `select_candidates`**

Add to `investor_agent.py` (top-level, near the existing imports add `import datetime as dt`, `import json`, `import os` if absent):

```python
BUY_CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), ".cache",
                                   "buy_candidates.json")


def _merge_pool(results: dict) -> list:
    """Merge rows across strategies → deduped pool, best rank kept, strategies
    recorded. Consensus (appears in more strategies) sorts first, then best rank."""
    pool = {}
    for name, payload in results.items():
        for row in payload.get("rows", []):
            t = row.get("ticker")
            if not t:
                continue
            entry = pool.setdefault(t, {"ticker": t, "strategies": [],
                                        "best_rank": 10**9, "score": None})
            entry["strategies"].append(name)
            entry["best_rank"] = min(entry["best_rank"], int(row.get("rank", 10**9)))
            sc = row.get("score")
            if isinstance(sc, (int, float)):
                entry["score"] = sc if entry["score"] is None else max(entry["score"], sc)
    ranked = sorted(pool.values(),
                    key=lambda e: (-len(e["strategies"]), e["best_rank"]))
    return ranked


def _build_prompt(per_strategy: dict, pool: list, top_n: int) -> str:
    lines = ["You are a portfolio analyst. Pick the best BUY candidates.",
             f"Return STRICT JSON: {{\"picks\":[{{\"ticker\":..,\"rationale\":\"<=15 words\"}}]}} with EXACTLY {top_n} picks.",
             "Only choose tickers from this candidate pool:"]
    for e in pool:
        lines.append(f"  {e['ticker']} (strategies: {','.join(e['strategies'])}, best_rank {e['best_rank']})")
    lines.append("\nPer-strategy lists:")
    for name, payload in per_strategy.items():
        tickers = ", ".join(r["ticker"] for r in payload.get("rows", [])[:10])
        lines.append(f"  {name}: {tickers}")
    return "\n".join(lines)


def _default_llm(prompt: str):
    """Call the local claude CLI; None on any failure."""
    if shutil.which("claude") is None:
        _log.warning("select_candidates: `claude` CLI not on PATH")
        return None
    try:
        result = subprocess.run(["claude", "-p", prompt], capture_output=True,
                                text=True, timeout=_CLAUDE_TIMEOUT_SEC)
    except Exception as e:
        _log.warning("select_candidates: claude call failed: %s", e)
        return None
    if result.returncode != 0:
        _log.warning("select_candidates: claude exited %d", result.returncode)
        return None
    return result.stdout


def _parse_llm(text, valid_tickers, top_n) -> "list | None":
    """Parse {'picks':[{ticker,rationale}]}; None if unusable."""
    if not text:
        return None
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        picks = data["picks"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    out = []
    for p in picks:
        t = p.get("ticker")
        if t in valid_tickers:
            out.append({"ticker": t, "rationale": str(p.get("rationale", ""))[:120]})
    if len(out) < top_n:
        return None
    return out[:top_n]


def select_candidates(top_n=None, owned=None, llm_fn=None) -> list:
    """Review all strategy results, pick top_n buy candidates, persist them."""
    import config
    import strategies
    top_n = top_n if top_n is not None else config.ENSEMBLE_TOP_N
    llm_fn = llm_fn or _default_llm
    if owned is None:
        try:
            import orders
            owned = {p["symbol"] for p in orders._load_portfolio_cache().get("positions", [])}
        except Exception:
            owned = set()

    results = strategies.load_strategy_results()
    pool = [e for e in _merge_pool(results) if e["ticker"] not in owned]

    picks = None
    if pool:
        valid = {e["ticker"] for e in pool}
        by_ticker = {e["ticker"]: e for e in pool}
        parsed = _parse_llm(llm_fn(_build_prompt(results, pool, top_n)), valid, top_n)
        if parsed is not None:
            picks = [{"ticker": p["ticker"], "rationale": p["rationale"],
                      "strategies": by_ticker[p["ticker"]]["strategies"]}
                     for p in parsed]
        else:
            picks = [{"ticker": e["ticker"], "rationale": "rule-ranked fallback",
                      "strategies": e["strategies"]} for e in pool[:top_n]]
    picks = picks or []

    os.makedirs(os.path.dirname(BUY_CANDIDATES_PATH), exist_ok=True)
    tmp = BUY_CANDIDATES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "picks": picks}, f)
    os.replace(tmp, BUY_CANDIDATES_PATH)
    return picks
```

If `import datetime as dt`, `import json`, `import os`, `import shutil` are not already at the top of `investor_agent.py`, add the missing ones.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_investor_agent.py -k select -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the whole investor_agent suite (no regression to existing review fn)**

Run: `python3 -m pytest tests/test_investor_agent.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add investor_agent.py tests/test_investor_agent.py
git commit -m "feat(investor_agent): cross-strategy top-N candidate selection"
```

---

### Task 6: Watchdog candidate source switch

**Files:**
- Modify: `watchdog.py` (`_get_screened_stocks`, ~line 1143)
- Test: `tests/test_watchdog.py`

**Interfaces:**
- Consumes: `investor_agent.BUY_CANDIDATES_PATH`.
- Produces: `_get_screened_stocks()` returns a DataFrame with a `ticker` column sourced from `buy_candidates.json` when present and non-empty, else the existing screener cache/run.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_watchdog.py`:

```python
def test_get_screened_stocks_prefers_buy_candidates(tmp_path, monkeypatch):
    import json, watchdog, investor_agent
    path = tmp_path / "buy_candidates.json"
    path.write_text(json.dumps({"generated_at": "x", "picks": [
        {"ticker": "AAA", "rationale": "r", "strategies": ["value"]},
        {"ticker": "BBB", "rationale": "r", "strategies": ["canslim"]},
    ]}))
    monkeypatch.setattr(investor_agent, "BUY_CANDIDATES_PATH", str(path))

    df = watchdog._get_screened_stocks()
    assert list(df["ticker"]) == ["AAA", "BBB"]


def test_get_screened_stocks_falls_back_to_screener(tmp_path, monkeypatch):
    import investor_agent, watchdog
    import pandas as pd
    monkeypatch.setattr(investor_agent, "BUY_CANDIDATES_PATH",
                        str(tmp_path / "missing.json"))
    monkeypatch.setattr(watchdog, "_load_screener_cache", lambda: None)
    monkeypatch.setattr("screener.screen_stocks", lambda: pd.DataFrame([{"ticker": "ZZZ"}]))
    df = watchdog._get_screened_stocks()
    assert list(df["ticker"]) == ["ZZZ"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_watchdog.py -k get_screened_stocks -v`
Expected: FAIL — the buy-candidates branch doesn't exist yet (first test fails).

- [ ] **Step 3: Implement the source switch**

Replace `_get_screened_stocks` in `watchdog.py` (currently at ~line 1143):

```python
def _get_screened_stocks() -> "pd.DataFrame":
    """Candidate source for check_buy_signals.

    Prefers the ensemble agent's vetted top-N (.cache/buy_candidates.json); if
    that's absent/empty, falls back to the raw screener (1-hour cache or fresh).
    """
    import investor_agent
    try:
        if os.path.exists(investor_agent.BUY_CANDIDATES_PATH):
            with open(investor_agent.BUY_CANDIDATES_PATH) as f:
                data = json.load(f)
            picks = data.get("picks", [])
            if picks:
                return pd.DataFrame(
                    [{"ticker": p["ticker"]} for p in picks if p.get("ticker")])
    except (OSError, json.JSONDecodeError, KeyError):
        pass  # fall through to screener

    cached = _load_screener_cache()
    if cached is not None and not cached.empty:
        return cached
    from screener import screen_stocks
    df = screen_stocks()
    if not df.empty:
        _save_screener_cache(df)
    return df
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_watchdog.py -k get_screened_stocks -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the buy-signal watchdog tests (no regression)**

Run: `python3 -m pytest tests/test_watchdog.py -k "buy_signal or screened" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): source buy candidates from ensemble agent picks"
```

---

### Task 7: Cron wiring + README + end-to-end dry run

**Files:**
- Modify: `README.md`
- Create: `run_ensemble.py` (tiny daily entrypoint: run strategies → select candidates)

**Interfaces:**
- Consumes: `strategies.run_strategies`, `strategies.default_registry`, `investor_agent.select_candidates`.

- [ ] **Step 1: Create the daily entrypoint**

Create `run_ensemble.py`:

```python
#!/usr/bin/env python3
"""Daily ensemble pipeline: run each strategy in isolation, then have the agent
pick the top-N buy candidates. Run before market open; the intraday watchdog
reads .cache/buy_candidates.json and decides entries.
"""
import logging
import strategies
import investor_agent

logging.basicConfig(level=logging.INFO)


def main():
    paths = strategies.run_strategies(strategies.default_registry())
    print(f"strategies written: {paths}")
    picks = investor_agent.select_candidates()
    for p in picks:
        print(f"  {p['ticker']:<6} [{','.join(p['strategies'])}] {p['rationale']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: End-to-end dry run with injected data**

Run:

```bash
python3 -c "
import strategies, investor_agent
strategies.write_strategy_result('value', [{'ticker':'AAA','score':2.0,'rank':1,'factors':{}}])
strategies.write_strategy_result('canslim', [{'ticker':'AAA','score':9.0,'rank':1,'factors':{}},{'ticker':'BBB','score':8.0,'rank':2,'factors':{}}])
picks = investor_agent.select_candidates(owned=set(), llm_fn=lambda p: None)
print('picks:', [(x['ticker'], x['strategies']) for x in picks])
"
```

Expected: prints picks led by `AAA` (consensus), with `strategies` listing both. (Writes real `.cache/` files — acceptable for a manual dry run.)

- [ ] **Step 3: Add the cron entry**

Add to the crontab (via `crontab -l > /tmp/ct; edit; crontab - < /tmp/ct`), before the rebalancer line, so candidates are fresh for the day:

```
# Ensemble: run strategies + agent pick (8:45 AM ET weekdays = 20:45 local)
45 20 * * 1-5 /Users/zl/works/stock/scripts/cron-wrapper.sh run_ensemble.py >> /Users/zl/works/stock/.cache/ensemble.log 2>&1
```

- [ ] **Step 4: Update README**

Add an "Ensemble strategy pipeline" subsection under Modules/Commands documenting: isolated strategies (`value_screen.py`, CANSLIM adapter) → `.cache/strategies/*.json`; `investor_agent.select_candidates()` → `.cache/buy_candidates.json` (top `ENSEMBLE_TOP_N`, rule fallback on LLM failure); `watchdog._get_screened_stocks()` reads it; `run_ensemble.py` daily entrypoint + cron; the `VS_*`/`ENSEMBLE_*` config flags.

Run: `grep -n "Ensemble strategy pipeline" README.md`
Expected: the new heading is present.

- [ ] **Step 5: Commit**

```bash
git add run_ensemble.py README.md
git commit -m "feat(ensemble): daily entrypoint + cron + docs"
```

---

## Self-Review

**Spec coverage:**
- Isolated strategies + contract → Tasks 2, 3, 4. ✓
- value+quality screen → Task 3. ✓
- One agent picks top 4 (+ rule fallback) → Task 5. ✓
- Watchdog reads top-4, auto-buy unchanged → Task 6. ✓
- Config → Task 1. ✓
- Runner/cron/README → Tasks 4 (registry), 7. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code; commands have expected output. ✓

**Type consistency:** Strategy rows `{ticker, score, rank, factors}` consistent across Tasks 2–5; `write_strategy_result(name, rows)` / `load_strategy_results()` / `run_strategies(registry)` / `default_registry()` signatures consistent; `select_candidates(top_n, owned, llm_fn)` and `BUY_CANDIDATES_PATH` match Task 6's consumer; buy-candidates schema `{generated_at, picks:[{ticker,rationale,strategies}]}` consistent between Tasks 5, 6, 7. ✓
