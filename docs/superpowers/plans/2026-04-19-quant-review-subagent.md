# Quant Review Subagent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily LLM-driven strategy reviewer that runs via a Claude Code scheduled remote trigger 3 hours after US-market close, fetches five external positioning signals, proposes parameter changes within a risk-tiered allowlist, and reports everything via Telegram.

**Architecture:** The trigger is a CC remote agent (no Python cron, no Anthropic API key). The Python codebase provides three CLI helper scripts the agent runs via Bash (`quant_fetch_portfolio.py`, `quant_fetch_externals.py`, `quant_apply.py`) plus a library (`quant/` package) with data fetchers, schema dataclasses, and an applier that enforces the risk tiers. `config.py` gets a single-block override-loader appended so overrides take effect next rebalance.

**Tech Stack:** Python 3.9+, existing `alpaca-py`, `yfinance`, `pandas`, stdlib `urllib` for HTTP, `xml.etree.ElementTree` for SEC filings. No new external dependencies (no Anthropic SDK — uses CC subscription).

**Spec:** `docs/superpowers/specs/2026-04-19-quant-review-subagent-design.md`

---

## File Structure

**New files:**
- `quant/__init__.py` — package marker
- `quant/schema.py` — `ExternalSignal`, `ProposedChange`, `QuantReview`, `ApplierResult` dataclasses
- `quant/data_sources.py` — 5 fetcher functions + `fetch_all_externals` parallel orchestrator
- `quant/applier.py` — risk-tier classification, bounds enforcement, file I/O, TG formatter
- `quant/trigger_prompt.md` — canonical trigger prompt (version-controlled source; trigger itself lives on Anthropic's side)
- `scripts/quant_fetch_portfolio.py` — CLI: portfolio state JSON to stdout
- `scripts/quant_fetch_externals.py` — CLI: 5-signal JSON to stdout
- `scripts/quant_apply.py` — CLI: takes proposals path, runs applier, writes outputs
- `.cache/strategy_overrides.json` — runtime: active overrides (created on first use)
- `.cache/strategy_proposals.json` — runtime: pending high-risk queue
- `.cache/proposed_changes.json` — runtime: agent's intermediate output
- `.cache/quant_review.log` — runtime: append-only audit log

**Modified files:**
- `config.py` — append override-loader block at end
- `README.md` — document the quant review flow + ops instructions

**Test files:**
- `tests/test_config_overrides.py` — override-loader paths
- `tests/test_quant_schema.py` — dataclass roundtrips
- `tests/test_quant_data_sources.py` — 5 fetchers with mocked HTTP
- `tests/test_quant_applier.py` — risk-tier classification + bounds + file I/O
- `tests/test_quant_cli_scripts.py` — subprocess invocations of the three CLI scripts
- `tests/test_quant_integration.py` — opt-in end-to-end test with canned agent output

---

## Task 1: Config override loader

**Files:**
- Modify: `config.py` (append at end)
- Create: `tests/test_config_overrides.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config_overrides.py`:

```python
import importlib
import json
import os


def _reload_config(tmp_path, monkeypatch, overrides=None):
    """Helper: point config's override path at tmp and reload the module."""
    import config
    override_path = tmp_path / "strategy_overrides.json"
    if overrides is not None:
        override_path.write_text(json.dumps(overrides))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(override_path))
    # Re-run just the loader block by calling the internal _apply_overrides
    # helper (which we expose via the module) — see implementation for name.
    config._apply_overrides()
    return config


def test_valid_stop_loss_override_applies(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    cfg = _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": 0.075})
    assert cfg.STOP_LOSS_PCT == 0.075
    # Cleanup: reset to repo default
    config.STOP_LOSS_PCT = original


def test_unknown_key_is_ignored(tmp_path, monkeypatch, caplog):
    import config
    original_max_orders = config.DAILY_MAX_ORDERS
    _reload_config(tmp_path, monkeypatch, {"DAILY_MAX_ORDERS": 999999})
    # Forbidden key → ignored; original value preserved.
    assert config.DAILY_MAX_ORDERS == original_max_orders


def test_type_mismatch_is_ignored(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": "not a float"})
    assert config.STOP_LOSS_PCT == original


def test_out_of_bounds_is_ignored(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": 0.99})  # above 0.20 cap
    assert config.STOP_LOSS_PCT == original


def test_missing_file_leaves_defaults_intact(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    # Point at a non-existent file
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(tmp_path / "nope.json"))
    config._apply_overrides()
    assert config.STOP_LOSS_PCT == original


def test_corrupt_json_leaves_defaults_intact(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(p))
    config._apply_overrides()
    assert config.STOP_LOSS_PCT == original


def test_watchlist_and_keywords_lists_apply(tmp_path, monkeypatch):
    import config
    _reload_config(tmp_path, monkeypatch, {
        "WATCHLIST": config.WATCHLIST + ["PLTR", "SMCI"],
        "NEWS_SHOCK_KEYWORDS": config.NEWS_SHOCK_KEYWORDS + ["nvda"],
    })
    assert "PLTR" in config.WATCHLIST
    assert "SMCI" in config.WATCHLIST
    assert "nvda" in config.NEWS_SHOCK_KEYWORDS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config_overrides.py -v`
Expected: FAIL with `AttributeError: module 'config' has no attribute '_OVERRIDES_PATH'` (or `_apply_overrides`).

- [ ] **Step 3: Append override-loader block to `config.py`**

Append at the end of `config.py`:

```python
# ── Strategy overrides (written by quant review subagent) ────────
import logging as _logging

_OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), ".cache", "strategy_overrides.json")

# Allowlist: key → (expected_type, lower_bound, upper_bound)
# Lower/upper bounds of None mean unbounded (for lists).
# The applier enforces relative-pct bounds (±20%, ±50%); this layer enforces
# absolute bounds as a second line of defense.
_OVERRIDE_SCHEMA = {
    "WATCHLIST":            (list,  None, None),
    "NEWS_SHOCK_KEYWORDS":  (list,  None, None),
    "STOP_LOSS_PCT":        (float, 0.04, 0.20),
    "TRAILING_STOP_PCT":    (float, 0.06, 0.25),
    "CASH_BUFFER_PCT":      (float, 0.02, 0.20),
}

def _apply_overrides():
    """Load and apply strategy overrides. Silent on missing/corrupt files.
    Called once at module import time (below)."""
    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH) as f:
            overrides = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _logging.warning(f"config: strategy_overrides.json unreadable ({e}); using defaults")
        return
    if not isinstance(overrides, dict):
        _logging.warning(f"config: strategy_overrides.json not an object; using defaults")
        return
    for key, value in overrides.items():
        if key not in _OVERRIDE_SCHEMA:
            _logging.warning(f"config: ignoring override for unknown/forbidden key {key!r}")
            continue
        expected_type, lo, hi = _OVERRIDE_SCHEMA[key]
        if not isinstance(value, expected_type):
            _logging.warning(f"config: override for {key!r} has wrong type {type(value).__name__}")
            continue
        if lo is not None and not (lo <= value <= hi):
            _logging.warning(f"config: override for {key!r}={value} out of bounds [{lo},{hi}]")
            continue
        globals()[key] = value

import json  # noqa: E402 — imports at bottom for _apply_overrides
_apply_overrides()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config_overrides.py -v`
Expected: all 7 tests PASS.

Run the full suite: `python3 -m pytest -v`
Expected: all prior tests still pass, +7 new.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config_overrides.py
git commit -m "feat: config.py reads .cache/strategy_overrides.json with allowlist

Second layer of defense: even if applier is bypassed, only these keys can
be overridden: WATCHLIST, NEWS_SHOCK_KEYWORDS, STOP_LOSS_PCT,
TRAILING_STOP_PCT, CASH_BUFFER_PCT. Type + bounds checked at load time.
Corrupt JSON / missing file / unknown key all fall back to defaults
with a warning."
```

---

## Task 2: Schema dataclasses

**Files:**
- Create: `quant/__init__.py` (empty)
- Create: `quant/schema.py`
- Create: `tests/test_quant_schema.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_quant_schema.py`:

```python
import datetime as dt
import json
from dataclasses import asdict


def test_external_signal_roundtrip():
    from quant.schema import ExternalSignal
    s = ExternalSignal(
        source="13F",
        as_of=dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc),
        data=[{"fund": "Berkshire", "ticker": "AAPL", "weight": 0.23}],
        error=None,
    )
    d = asdict(s)
    d["as_of"] = s.as_of.isoformat()
    blob = json.dumps(d)
    back = json.loads(blob)
    assert back["source"] == "13F"
    assert back["data"][0]["ticker"] == "AAPL"
    assert back["error"] is None


def test_external_signal_error_case():
    from quant.schema import ExternalSignal
    s = ExternalSignal(
        source="reddit",
        as_of=dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc),
        data=[],
        error="connection refused",
    )
    assert s.data == []
    assert s.error == "connection refused"


def test_proposed_change_fields():
    from quant.schema import ProposedChange
    c = ProposedChange(
        key="STOP_LOSS_PCT",
        current_value=0.08,
        proposed_value=0.075,
        rationale="ATR compressed 30%",
        detailed_plan="Next rebalance attaches 7.5% stops",
        expected_effect="cuts losers 15% faster",
        risk_tier="low",
        confidence=0.70,
    )
    assert c.risk_tier == "low"
    assert c.confidence == 0.70


def test_proposed_change_rejects_invalid_risk_tier():
    """risk_tier is a literal; invalid values should fail in downstream validation.
    The dataclass itself doesn't enforce — applier does. This test pins that
    invalid values at least construct (we deal with them in applier)."""
    from quant.schema import ProposedChange
    c = ProposedChange(
        key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075,
        rationale="r", detailed_plan="p", expected_effect="e",
        risk_tier="medium",  # invalid per design, but dataclass allows
        confidence=0.5,
    )
    assert c.risk_tier == "medium"  # applier will reject this


def test_quant_review_requires_no_changes_reason_when_empty():
    """QuantReview allows empty proposed_changes only if no_changes_reason is set."""
    from quant.schema import QuantReview
    r = QuantReview(
        date="2026-04-19",
        portfolio_summary="baseline",
        macro_read="risk-on",
        reasoning_summary="nothing new",
        data_gaps=[],
        proposed_changes=[],
        no_changes_reason="all signals confirm current strategy",
    )
    assert r.proposed_changes == []
    assert r.no_changes_reason is not None


def test_applier_result_defaults():
    from quant.schema import ApplierResult
    r = ApplierResult()
    assert r.applied_low == []
    assert r.queued_high == []
    assert r.rejected_forbidden == []
    assert r.rejected_out_of_bounds == []
    assert r.rejected_malformed == []
```

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_quant_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'quant'`.

- [ ] **Step 3: Create `quant/__init__.py`**

```python
```

(empty file)

- [ ] **Step 4: Create `quant/schema.py`**

```python
"""Shared dataclasses for the quant review subagent.

All types use plain dataclasses + JSON-serializable primitive fields so they
move cleanly between the agent (JSON on stdout/in files) and Python (applier
reads + validates).
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ExternalSignal:
    """One of the five external data feeds, normalized."""
    source: str                    # "13F" | "reddit" | "etf-holdings" | "ark" | "congress"
    as_of: dt.datetime             # freshness timestamp
    data: list                     # source-specific rows (list of dicts)
    error: Optional[str] = None    # set if fetch failed; data=[] in that case


@dataclass(frozen=True)
class ProposedChange:
    """A single parameter change the agent wants to make."""
    key: str                       # config key name (must be in allowlist at apply time)
    current_value: Any             # echo of current value (agent reads from prompt)
    proposed_value: Any
    rationale: str                 # paragraph: why — must cite specific data
    detailed_plan: str             # paragraph: what happens — concrete portfolio effect
    expected_effect: str           # short: e.g. "cuts losers 15% faster"
    risk_tier: str                 # "low" | "high" — agent pre-classifies; applier verifies
    confidence: float              # 0..1


@dataclass(frozen=True)
class QuantReview:
    """Top-level review object the agent produces."""
    date: str                      # ISO date
    portfolio_summary: str
    macro_read: str
    reasoning_summary: str
    data_gaps: list                # list[str]
    proposed_changes: list         # list[ProposedChange]
    no_changes_reason: Optional[str] = None


@dataclass
class ApplierResult:
    """Outcome of running the applier over a review's proposed changes."""
    applied_low: list = field(default_factory=list)            # list[ProposedChange]
    queued_high: list = field(default_factory=list)            # list[ProposedChange]
    rejected_forbidden: list = field(default_factory=list)     # list[ProposedChange]
    rejected_out_of_bounds: list = field(default_factory=list) # list[ProposedChange]
    rejected_malformed: list = field(default_factory=list)     # list[dict]
```

- [ ] **Step 5: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_schema.py -v`
Expected: all 6 tests PASS.

Full suite: `python3 -m pytest -v` — no regressions.

- [ ] **Step 6: Commit**

```bash
git add quant/__init__.py quant/schema.py tests/test_quant_schema.py
git commit -m "feat: quant/schema.py — dataclasses for review subagent I/O"
```

---

## Task 3: 13F filings fetcher

**Files:**
- Create: `quant/data_sources.py` (this task creates the file; subsequent tasks extend it)
- Create: `tests/test_quant_data_sources.py` (likewise)

- [ ] **Step 1: Write failing test**

Create `tests/test_quant_data_sources.py`:

```python
import datetime as dt
from unittest.mock import patch, MagicMock


def test_fetch_13f_returns_external_signal_on_empty_response():
    """When SEC endpoints return nothing, we get an ExternalSignal with
    data=[] — never a raise."""
    from quant.data_sources import fetch_13f_filings
    from quant.schema import ExternalSignal

    with patch("quant.data_sources._fetch_latest_13f_for_cik", return_value=None):
        sig = fetch_13f_filings()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "13F"
    assert sig.data == []
    # Some sources may not have an as_of when empty — allow either a stamp or None


def test_fetch_13f_aggregates_across_funds():
    from quant.data_sources import fetch_13f_filings

    def fake_fetch(cik):
        mapping = {
            "0001067983": {
                "period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "AAPL", "value": 150_000_000, "weight": 0.23}],
            },
            "0001336528": {
                "period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "MSFT", "value": 80_000_000, "weight": 0.08}],
            },
        }
        return mapping.get(cik)

    with patch("quant.data_sources._fetch_latest_13f_for_cik", side_effect=fake_fetch), \
         patch("quant.data_sources._TRACKED_13F_FUNDS", {
             "0001067983": "Berkshire Hathaway",
             "0001336528": "Bridgewater",
         }):
        sig = fetch_13f_filings()
    assert sig.source == "13F"
    assert sig.error is None
    symbols = {row["ticker"] for row in sig.data}
    assert symbols == {"AAPL", "MSFT"}
    funds = {row["fund"] for row in sig.data}
    assert funds == {"Berkshire Hathaway", "Bridgewater"}


def test_fetch_13f_tolerates_per_fund_errors():
    """If one fund's fetch fails, others still return."""
    from quant.data_sources import fetch_13f_filings

    def fake_fetch(cik):
        if cik == "broken":
            raise RuntimeError("SEC returned 500")
        return {"period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "AAPL", "value": 1, "weight": 0.01}]}

    with patch("quant.data_sources._fetch_latest_13f_for_cik", side_effect=fake_fetch), \
         patch("quant.data_sources._TRACKED_13F_FUNDS", {
             "0001067983": "Berkshire",
             "broken": "BrokenFund",
         }):
        sig = fetch_13f_filings()
    # Healthy funds still in data; broken one silently skipped.
    assert any(row["fund"] == "Berkshire" for row in sig.data)
    assert not any(row["fund"] == "BrokenFund" for row in sig.data)


def test_fetch_13f_returns_error_signal_on_total_failure():
    """If ALL funds fail, return an ExternalSignal with error set."""
    from quant.data_sources import fetch_13f_filings

    with patch("quant.data_sources._fetch_latest_13f_for_cik",
               side_effect=RuntimeError("network down")):
        sig = fetch_13f_filings()
    assert sig.data == []
    assert sig.error is not None
```

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v`
Expected: FAIL with `ModuleNotFoundError` on `quant.data_sources`.

- [ ] **Step 3: Create `quant/data_sources.py` with 13F support**

```python
"""External data-signal fetchers for the quant review subagent.

Five sources; each returns a normalized ExternalSignal. Fetchers never raise —
network errors, parsing failures, empty responses all yield a signal with
`error` populated and `data=[]`.

This file is built incrementally: Task 3 adds fetch_13f_filings; Tasks 4-7
add reddit / etf-holdings / ark / congress; Task 8 adds fetch_all_externals
as a parallel orchestrator.
"""
from __future__ import annotations
import datetime as dt
import logging
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
_SEC_USER_AGENT = "stock-tracker research contact@example.com"   # SEC requires a UA


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
    or None if no 13F found. Raises on unrecoverable errors (caller catches)."""
    import json as _json
    # 1. Get submission history
    submissions_url = f"{_SEC_BASE}/submissions/CIK{cik.zfill(10)}.json"
    raw = _sec_get(submissions_url)
    submissions = _json.loads(raw)
    recent = submissions["filings"]["recent"]
    # 2. Find most recent 13F-HR
    form_types = recent["form"]
    accession_numbers = recent["accessionNumber"]
    filing_dates = recent["filingDate"]
    primary_docs = recent["primaryDocument"]
    report_dates = recent["reportDate"]
    for idx, form in enumerate(form_types):
        if form == "13F-HR":
            accession = accession_numbers[idx].replace("-", "")
            doc = primary_docs[idx]
            # 3. Fetch the info-table XML that lives alongside the primary doc.
            #    The primary doc is typically an HTML cover; the holdings are in
            #    `{accession}.xml` or `form13fInfoTable.xml`. We try the common
            #    filename patterns.
            base = f"{_SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession}"
            for candidate in ("form13fInfoTable.xml", "infotable.xml",
                              doc.replace(".htm", ".xml")):
                try:
                    xml_bytes = _sec_get(f"{base}/{candidate}", timeout=20)
                    holdings = _parse_13f_info_table(xml_bytes)
                    # Weight by dollar value
                    total = sum(h["value"] for h in holdings)
                    for h in holdings:
                        h["weight"] = h["value"] / total if total else 0.0
                    holdings.sort(key=lambda h: h["value"], reverse=True)
                    return {
                        "period_of_report": dt.date.fromisoformat(report_dates[idx]),
                        "top_20": holdings[:20],
                    }
                except urllib.error.HTTPError:
                    continue
            return None
    return None


def _parse_13f_info_table(xml_bytes: bytes) -> list:
    """Parse a form13fInfoTable.xml into [{"ticker": cusip or name, "value": int}]."""
    ns = {"n": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}
    tree = ET.fromstring(xml_bytes)
    rows = []
    for info in tree.findall("n:infoTable", ns):
        name = (info.findtext("n:nameOfIssuer", default="", namespaces=ns) or "").strip()
        cusip = (info.findtext("n:cusip", default="", namespaces=ns) or "").strip()
        value_raw = info.findtext("n:value", default="0", namespaces=ns)
        try:
            value = int(float(value_raw)) * 1000   # 13F reports value in thousands
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
    if not rows and errors:
        return ExternalSignal(
            source="13F",
            as_of=dt.datetime.now(dt.timezone.utc),
            data=[],
            error="; ".join(errors[:3]),
        )
    as_of = (dt.datetime.combine(latest_date, dt.time()).replace(tzinfo=dt.timezone.utc)
             if latest_date else dt.datetime.now(dt.timezone.utc))
    return ExternalSignal(source="13F", as_of=as_of, data=rows)
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: quant/data_sources.py — 13F filings fetcher

Aggregates top-20 holdings from 6 tracked funds' most recent 13F-HR
filings via SEC EDGAR. Fetcher tolerates per-fund failures (returns the
healthy ones); returns an error-stamped ExternalSignal only if all funds
fail. XML parsing uses stdlib ElementTree — no new dependency."
```

---

## Task 4: Reddit trending fetcher

**Files:**
- Modify: `quant/data_sources.py` (append fetcher)
- Modify: `tests/test_quant_data_sources.py` (append tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_quant_data_sources.py`:

```python
def test_fetch_reddit_trending_returns_tickers():
    from quant.data_sources import fetch_reddit_trending
    from quant.schema import ExternalSignal

    fake_posts = [
        {"title": "NVDA to the moon!", "score": 500, "ts": 1713500000,
         "subreddit": "wallstreetbets"},
        {"title": "Bought more $TSLA calls, going long", "score": 200, "ts": 1713500100,
         "subreddit": "wallstreetbets"},
        {"title": "Shorting AAPL before earnings", "score": 50, "ts": 1713500200,
         "subreddit": "stocks"},
    ]
    # Mock the internal helper that returns raw posts
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_reddit_hot_posts", return_value=fake_posts):
        sig = fetch_reddit_trending()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "reddit"
    tickers = {row["ticker"] for row in sig.data}
    # At minimum NVDA and TSLA should be picked up
    assert "NVDA" in tickers
    assert "TSLA" in tickers


def test_fetch_reddit_trending_handles_network_failure():
    from quant.data_sources import fetch_reddit_trending
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_reddit_hot_posts",
               side_effect=RuntimeError("blocked")):
        sig = fetch_reddit_trending()
    assert sig.data == []
    assert sig.error is not None
```

- [ ] **Step 2: Verify new tests fail**

Run: `python3 -m pytest tests/test_quant_data_sources.py::test_fetch_reddit_trending_returns_tickers -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Append fetcher to `quant/data_sources.py`**

```python
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

# Words that look like tickers but aren't — avoid false positives.
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
    are filtered later by cross-checking against a known universe."""
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
    counts: dict = {}   # ticker -> {"count": int, "titles": list}
    for post in posts:
        title = post.get("title", "")
        for ticker in _extract_tickers(title):
            rec = counts.setdefault(ticker, {"count": 0, "titles": []})
            rec["count"] += 1
            if len(rec["titles"]) < 3:
                rec["titles"].append(title[:100])
    # Build normalized rows sorted by mention count
    rows = [{"ticker": t, "mentions": rec["count"], "sample_titles": rec["titles"]}
            for t, rec in counts.items()]
    rows.sort(key=lambda r: r["mentions"], reverse=True)
    return ExternalSignal(
        source="reddit",
        as_of=dt.datetime.now(dt.timezone.utc),
        data=rows[:20],
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: quant reddit fetcher — ticker mentions from finance subs"
```

---

## Task 5: Popular ETF holdings fetcher

**Files:**
- Modify: `quant/data_sources.py`
- Modify: `tests/test_quant_data_sources.py`

- [ ] **Step 1: Append failing tests**

```python
def test_fetch_etf_holdings_normalizes_rows():
    from quant.data_sources import fetch_popular_etf_holdings
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import pandas as pd

    fake_holdings = pd.DataFrame({
        "symbol": ["AAPL", "MSFT", "NVDA"],
        "holdingPercent": [0.15, 0.12, 0.08],
    })
    with patch("quant.data_sources._fetch_etf_top_holdings", return_value=fake_holdings):
        sig = fetch_popular_etf_holdings()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "etf-holdings"
    # Each row has etf + ticker + weight
    first = sig.data[0]
    assert "etf" in first and "ticker" in first and "weight" in first


def test_fetch_etf_holdings_tolerates_missing_etfs():
    from quant.data_sources import fetch_popular_etf_holdings
    from unittest.mock import patch

    def fake_fetch(symbol):
        if symbol == "ARKK":
            raise RuntimeError("not found")
        import pandas as pd
        return pd.DataFrame({"symbol": ["FOO"], "holdingPercent": [0.1]})

    with patch("quant.data_sources._fetch_etf_top_holdings", side_effect=fake_fetch):
        sig = fetch_popular_etf_holdings()
    # Other ETFs succeeded; ARKK just got skipped silently
    assert any(row["ticker"] == "FOO" for row in sig.data)
    # Error was not fatal
    assert sig.error is None or "ARKK" in sig.error
```

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v`
Expected: FAIL with ImportError on `fetch_popular_etf_holdings`.

- [ ] **Step 3: Append fetcher**

```python
# ── Popular ETFs ────────────────────────────────────────────────

_TRACKED_ETFS = ["MAGS", "ARKK", "QQQ", "ICLN", "VGT"]


def _fetch_etf_top_holdings(symbol: str):
    """Return a DataFrame of top holdings for an ETF via yfinance.
    yfinance exposes holdings through Ticker(...).funds_data.top_holdings in
    recent versions; falls back to Ticker(...).get_info()['holdings'] for older
    versions."""
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    funds_data = getattr(ticker, "funds_data", None)
    if funds_data is not None:
        th = getattr(funds_data, "top_holdings", None)
        if th is not None and not th.empty:
            return th.reset_index()
    # Fallback: best-effort
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
        # Normalize column names across yfinance variants
        symbol_col = next((c for c in df.columns if c.lower() in ("symbol", "ticker")), None)
        weight_col = next((c for c in df.columns if "percent" in c.lower() or c.lower() == "weight"),
                           None)
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
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: quant popular-ETF holdings fetcher (yfinance)"
```

---

## Task 6: ARK daily trades fetcher

**Files:**
- Modify: `quant/data_sources.py`
- Modify: `tests/test_quant_data_sources.py`

- [ ] **Step 1: Append failing tests**

```python
def test_fetch_ark_trades_parses_csv():
    from quant.data_sources import fetch_ark_trades
    from quant.schema import ExternalSignal
    from unittest.mock import patch

    fake_csv = (
        "date,fund,direction,ticker,company,shares,weight(%)\n"
        "4/18/2026,ARKK,Buy,TSLA,TESLA INC,12345,0.8\n"
        "4/18/2026,ARKG,Sell,CRSP,CRISPR THERA,5000,0.3\n"
    )
    with patch("quant.data_sources._fetch_ark_csv", return_value=fake_csv):
        sig = fetch_ark_trades()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "ark"
    dirs = {row["direction"] for row in sig.data}
    assert dirs == {"buy", "sell"}
    tickers = {row["ticker"] for row in sig.data}
    assert tickers == {"TSLA", "CRSP"}


def test_fetch_ark_trades_handles_fetch_failure():
    from quant.data_sources import fetch_ark_trades
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_ark_csv", side_effect=RuntimeError("404")):
        sig = fetch_ark_trades()
    assert sig.data == []
    assert sig.error is not None
```

- [ ] **Step 2: Verify tests fail**

- [ ] **Step 3: Append fetcher**

```python
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
```

- [ ] **Step 4: Verify + commit**

Run: `python3 -m pytest tests/test_quant_data_sources.py -v` — all PASS.

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: quant ARK daily-trades fetcher (CSV scrape)"
```

---

## Task 7: Congress / Pelosi trades fetcher

**Files:**
- Modify: `quant/data_sources.py`
- Modify: `tests/test_quant_data_sources.py`

- [ ] **Step 1: Append failing tests**

```python
def test_fetch_congress_trades_parses_json():
    from quant.data_sources import fetch_congress_trades
    from quant.schema import ExternalSignal
    from unittest.mock import patch

    fake_json = {
        "data": [
            {"politician": {"firstName": "Nancy", "lastName": "Pelosi"},
             "traded": "2026-04-15", "disclosed": "2026-04-17",
             "asset": {"ticker": "TSLA"},
             "type": "buy",
             "value": "$1,000,001 - $5,000,000"},
            {"politician": {"firstName": "Josh", "lastName": "Gottheimer"},
             "traded": "2026-04-12", "disclosed": "2026-04-14",
             "asset": {"ticker": "NVDA"},
             "type": "sell",
             "value": "$50,001 - $100,000"},
        ]
    }
    with patch("quant.data_sources._fetch_capitoltrades_json", return_value=fake_json):
        sig = fetch_congress_trades()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "congress"
    tickers = {row["ticker"] for row in sig.data}
    assert tickers == {"TSLA", "NVDA"}
    pelosi_row = next(r for r in sig.data if r["member"] == "Nancy Pelosi")
    assert pelosi_row["direction"] == "buy"


def test_fetch_congress_trades_handles_network_error():
    from quant.data_sources import fetch_congress_trades
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_capitoltrades_json",
               side_effect=RuntimeError("timeout")):
        sig = fetch_congress_trades()
    assert sig.data == []
    assert sig.error is not None
```

- [ ] **Step 2: Verify fail; Step 3: Append fetcher**

```python
# ── Congress / Pelosi ────────────────────────────────────────────

_CAPITOLTRADES_API = "https://bff.capitoltrades.com/trades"


def _fetch_capitoltrades_json(days: int = 14) -> dict:
    """Fetch recent disclosed trades from capitoltrades' public endpoint."""
    # API: ?page=1&pageSize=100 — they sort newest first. 100 is plenty for
    # a 14-day window across all tracked members.
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
```

- [ ] **Step 4: Verify + commit**

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: quant Congress/STOCK Act fetcher (capitoltrades)"
```

---

## Task 8: fetch_all_externals parallel orchestrator

**Files:**
- Modify: `quant/data_sources.py`
- Modify: `tests/test_quant_data_sources.py`

- [ ] **Step 1: Append failing test**

```python
def test_fetch_all_externals_returns_five_signals():
    from quant.data_sources import fetch_all_externals
    from quant.schema import ExternalSignal
    from unittest.mock import patch

    def stub(source):
        return lambda: ExternalSignal(
            source=source,
            as_of=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            data=[{"row": source}],
        )

    with patch("quant.data_sources.fetch_13f_filings", side_effect=stub("13F")), \
         patch("quant.data_sources.fetch_reddit_trending", side_effect=stub("reddit")), \
         patch("quant.data_sources.fetch_popular_etf_holdings",
               side_effect=stub("etf-holdings")), \
         patch("quant.data_sources.fetch_ark_trades", side_effect=stub("ark")), \
         patch("quant.data_sources.fetch_congress_trades", side_effect=stub("congress")):
        signals = fetch_all_externals()
    assert len(signals) == 5
    sources = {s.source for s in signals}
    assert sources == {"13F", "reddit", "etf-holdings", "ark", "congress"}


def test_fetch_all_externals_catches_fetcher_crash():
    """If a fetcher raises (not just returns an error signal), we still get
    back 5 signals — the crashed one has error populated."""
    from quant.data_sources import fetch_all_externals
    from unittest.mock import patch
    with patch("quant.data_sources.fetch_13f_filings", side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_reddit_trending",
               side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_popular_etf_holdings",
               side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_ark_trades", side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_congress_trades",
               side_effect=RuntimeError("boom")):
        signals = fetch_all_externals()
    assert len(signals) == 5
    for s in signals:
        assert s.error is not None
        assert s.data == []
```

- [ ] **Step 2: Verify fails; Step 3: Append orchestrator**

```python
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
```

- [ ] **Step 4: Verify + commit**

```bash
git add quant/data_sources.py tests/test_quant_data_sources.py
git commit -m "feat: fetch_all_externals — parallel orchestrator for 5 sources"
```

---

## Task 9: Applier — classification + bounds

**Files:**
- Create: `quant/applier.py`
- Create: `tests/test_quant_applier.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_quant_applier.py
from quant.schema import ProposedChange


def _change(**kwargs):
    defaults = dict(
        key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075,
        rationale="r", detailed_plan="p", expected_effect="e",
        risk_tier="low", confidence=0.7,
    )
    defaults.update(kwargs)
    return ProposedChange(**defaults)


def test_classify_stop_loss_within_band_is_low():
    from quant.applier import classify_change
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    assert classify_change(c) == "low"


def test_classify_stop_loss_out_of_band_is_high():
    from quant.applier import classify_change
    # +30% from 0.08 is 0.104, outside the ±20% band → high-risk
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.104)
    assert classify_change(c) == "high"


def test_classify_stop_loss_out_of_absolute_bounds_is_rejected():
    from quant.applier import classify_change
    # 0.50 is outside the absolute bound [0.04, 0.20]
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.50)
    assert classify_change(c) == "rejected_out_of_bounds"


def test_classify_momentum_top_n_is_always_high():
    from quant.applier import classify_change
    c = _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3)
    assert classify_change(c) == "high"


def test_classify_daily_max_orders_is_forbidden():
    from quant.applier import classify_change
    c = _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100)
    assert classify_change(c) == "forbidden"


def test_classify_watchlist_addition_is_low():
    from quant.applier import classify_change
    current = ["SPY", "QQQ"]
    proposed = current + ["PLTR"]
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "low"


def test_classify_watchlist_removal_is_high():
    from quant.applier import classify_change
    current = ["SPY", "QQQ", "IWM"]
    proposed = ["SPY", "QQQ"]   # removed IWM
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "high"


def test_classify_watchlist_over_size_cap_is_rejected():
    from quant.applier import classify_change
    current = [f"T{i}" for i in range(99)]
    proposed = current + ["NEW_ONE", "NEW_TWO"]    # >100
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "rejected_out_of_bounds"
```

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_quant_applier.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Create `quant/applier.py`**

```python
"""Applier: classifies proposed changes, enforces bounds, writes output files.

Risk-tier classification is the authoritative source of truth — the agent's
own `risk_tier` field is a hint, not a decision. Applier always re-classifies
from scratch using the same rules config.py's override loader enforces.
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
from typing import Any, Optional

import config
from quant.schema import ProposedChange, ApplierResult

LOG = logging.getLogger(__name__)


# ── Tier allowlists ──────────────────────────────────────────────

# Low-risk: auto-applied if within bounds.
_LOW_RISK_NUMERIC = {
    # key → (absolute_lo, absolute_hi, relative_pct_band)
    "STOP_LOSS_PCT":     (0.04, 0.20, 0.20),
    "TRAILING_STOP_PCT": (0.06, 0.25, 0.20),
    "CASH_BUFFER_PCT":   (0.02, 0.20, 0.50),
}

_LOW_RISK_LISTS = {
    # key → max size
    "WATCHLIST":            100,
    "NEWS_SHOCK_KEYWORDS":  30,
}

# High-risk: requires user approval.
_HIGH_RISK_KEYS = {
    "MOMENTUM_TOP_N",
    "ETF_ALLOCATION_PCT",
    "STOCK_ALLOCATION_PCT",
    "SCREEN_MIN_ROE",
    "SCREEN_MAX_PE",
    "SCREEN_MAX_DEBT_EQUITY",
    "MOMENTUM_LOOKBACK_MONTHS",
    "SAFE_HAVEN",
}

# Everything else is implicitly forbidden (default-deny).


def classify_change(change: ProposedChange) -> str:
    """Return one of: "low" | "high" | "forbidden" | "rejected_out_of_bounds"."""
    key = change.key
    proposed = change.proposed_value
    current = change.current_value

    # 1. Low-risk numeric keys
    if key in _LOW_RISK_NUMERIC:
        abs_lo, abs_hi, rel_band = _LOW_RISK_NUMERIC[key]
        if not isinstance(proposed, (int, float)):
            return "rejected_out_of_bounds"
        if not (abs_lo <= proposed <= abs_hi):
            return "rejected_out_of_bounds"
        if isinstance(current, (int, float)) and current > 0:
            rel = abs(proposed - current) / current
            if rel > rel_band:
                return "high"   # out of low-risk band → bumped up to high
        return "low"

    # 2. Low-risk list keys
    if key in _LOW_RISK_LISTS:
        max_size = _LOW_RISK_LISTS[key]
        if not isinstance(proposed, list) or not isinstance(current, list):
            return "rejected_out_of_bounds"
        if len(proposed) > max_size:
            return "rejected_out_of_bounds"
        current_set = set(current)
        proposed_set = set(proposed)
        if proposed_set < current_set:
            # Removal detected → high-risk
            return "high"
        return "low"

    # 3. High-risk keys
    if key in _HIGH_RISK_KEYS:
        return "high"

    # 4. Everything else: forbidden
    return "forbidden"
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_applier.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add quant/applier.py tests/test_quant_applier.py
git commit -m "feat: quant applier classification (low/high/forbidden/OOB)"
```

---

## Task 10: Applier — file I/O + TG notification

**Files:**
- Modify: `quant/applier.py`
- Modify: `tests/test_quant_applier.py`

- [ ] **Step 1: Append failing tests**

```python
def test_apply_writes_low_risk_to_overrides(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    proposals_path = tmp_path / "proposals.json"
    tg_path = tmp_path / "tg.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(proposals_path))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tg_path))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    result = applier.apply([c])

    assert len(result.applied_low) == 1
    import json as _j
    overrides = _j.loads(overrides_path.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075


def test_apply_writes_high_risk_to_proposals(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    proposals_path = tmp_path / "proposals.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(proposals_path))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3)
    result = applier.apply([c])

    assert len(result.queued_high) == 1
    import json as _j
    queue = _j.loads(proposals_path.read_text())
    assert queue[0]["key"] == "MOMENTUM_TOP_N"
    assert "id" in queue[0]
    assert "expires_at" in queue[0]


def test_apply_rejects_forbidden_and_records(tmp_path, monkeypatch):
    import quant.applier as applier
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(tmp_path / "o.json"))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100)
    result = applier.apply([c])

    assert len(result.rejected_forbidden) == 1
    # overrides.json not written (nothing to apply)
    assert not (tmp_path / "o.json").exists()


def test_apply_dry_run_writes_dry_artifact_only(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    dry_path = tmp_path / "dry.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(applier, "DRY_RUN_PATH", str(dry_path))

    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    result = applier.apply([c], dry_run=True)

    assert len(result.applied_low) == 1
    # In dry-run mode: overrides.json NOT written; dry artifact IS.
    assert not overrides_path.exists()
    assert dry_path.exists()


def test_tg_notification_contains_all_sections(tmp_path, monkeypatch):
    import quant.applier as applier
    tg_path = tmp_path / "tg.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(tmp_path / "o.json"))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tg_path))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    changes = [
        _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075),
        _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3),
        _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100),
    ]
    applier.apply(changes)
    import json as _j
    notifs = _j.loads(tg_path.read_text())
    # A single append of the most recent review (prepend if you want newest-first)
    assert len(notifs) >= 1
    latest = notifs[-1]
    assert "message" in latest
    msg = latest["message"]
    assert "AUTO-APPLIED" in msg
    assert "NEEDS YOUR APPROVAL" in msg
    assert "REJECTED" in msg
```

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_quant_applier.py -v`
Expected: FAIL — `apply` / path constants not yet defined.

- [ ] **Step 3: Append apply + TG formatter to `quant/applier.py`**

```python
# ── File paths (overridable for tests) ───────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
OVERRIDES_PATH = os.path.join(_CACHE_DIR, "strategy_overrides.json")
PROPOSALS_PATH = os.path.join(_CACHE_DIR, "strategy_proposals.json")
TG_NOTIFY_PATH = os.path.join(_CACHE_DIR, "telegram_notifications.json")
AUDIT_LOG_PATH = os.path.join(_CACHE_DIR, "quant_review.log")
DRY_RUN_PATH   = os.path.join(_CACHE_DIR, "quant_review_dry.json")


# ── Public API ───────────────────────────────────────────────────

def apply(
    changes: list,
    *,
    dry_run: bool = False,
    review_context: Optional[dict] = None,
) -> ApplierResult:
    """Classify + apply/queue/reject each proposed change.

    `review_context` is an optional dict with portfolio_summary, macro_read,
    reasoning_summary, data_gaps — used to enrich the TG report.

    In dry-run mode, nothing is written to strategy_overrides.json or
    strategy_proposals.json; instead a combined artifact goes to
    .cache/quant_review_dry.json. TG notification is still written so the
    user can see what would have happened."""
    result = ApplierResult()
    for change in changes:
        if not isinstance(change, ProposedChange):
            result.rejected_malformed.append({"raw": repr(change)})
            continue
        tier = classify_change(change)
        if tier == "low":
            result.applied_low.append(change)
        elif tier == "high":
            result.queued_high.append(change)
        elif tier == "forbidden":
            result.rejected_forbidden.append(change)
        else:
            result.rejected_out_of_bounds.append(change)

    if dry_run:
        _write_dry_run(changes, result)
    else:
        _merge_overrides(result.applied_low)
        _append_proposals(result.queued_high)

    _write_tg_notification(result, review_context)
    _append_audit_log(result, review_context, dry_run=dry_run)

    return result


# ── Helpers ──────────────────────────────────────────────────────

def _merge_overrides(low_changes: list) -> None:
    """Merge applied-low changes into strategy_overrides.json."""
    os.makedirs(os.path.dirname(OVERRIDES_PATH), exist_ok=True)
    existing = {}
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    for c in low_changes:
        existing[c.key] = c.proposed_value
    with open(OVERRIDES_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)


def _append_proposals(high_changes: list) -> None:
    """Append high-risk changes to strategy_proposals.json with expiry."""
    os.makedirs(os.path.dirname(PROPOSALS_PATH), exist_ok=True)
    existing = []
    if os.path.exists(PROPOSALS_PATH):
        try:
            with open(PROPOSALS_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = []
    now = dt.datetime.now(dt.timezone.utc)
    # Expire at 21:35 local today, which is 13:35 UTC if we're in +08
    expires = now.replace(hour=13, minute=35, second=0, microsecond=0)
    if expires < now:
        expires = expires + dt.timedelta(days=1)
    today_slug = now.date().isoformat()
    for idx, c in enumerate(high_changes, start=1 + len(existing)):
        existing.append({
            "id": f"prop_{today_slug}_{idx:02d}",
            "key": c.key,
            "current": c.current_value,
            "proposed": c.proposed_value,
            "rationale": c.rationale,
            "detailed_plan": c.detailed_plan,
            "expected_effect": c.expected_effect,
            "confidence": c.confidence,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        })
    with open(PROPOSALS_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)


def _write_dry_run(changes: list, result: ApplierResult) -> None:
    os.makedirs(os.path.dirname(DRY_RUN_PATH), exist_ok=True)
    data = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": True,
        "proposed": [_change_to_dict(c) for c in changes if isinstance(c, ProposedChange)],
        "classification": {
            "applied_low": [_change_to_dict(c) for c in result.applied_low],
            "queued_high": [_change_to_dict(c) for c in result.queued_high],
            "rejected_forbidden": [_change_to_dict(c) for c in result.rejected_forbidden],
            "rejected_out_of_bounds": [_change_to_dict(c) for c in result.rejected_out_of_bounds],
        },
    }
    with open(DRY_RUN_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _change_to_dict(c: ProposedChange) -> dict:
    return {
        "key": c.key,
        "current_value": c.current_value,
        "proposed_value": c.proposed_value,
        "rationale": c.rationale,
        "detailed_plan": c.detailed_plan,
        "expected_effect": c.expected_effect,
        "risk_tier": c.risk_tier,
        "confidence": c.confidence,
    }


def _write_tg_notification(result: ApplierResult,
                           review_context: Optional[dict]) -> None:
    """Append a formatted TG message to telegram_notifications.json."""
    os.makedirs(os.path.dirname(TG_NOTIFY_PATH), exist_ok=True)
    existing = []
    if os.path.exists(TG_NOTIFY_PATH):
        try:
            with open(TG_NOTIFY_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = []
    message = _format_tg_message(result, review_context)
    existing.append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "quant-review",
        "message": message,
    })
    with open(TG_NOTIFY_PATH, "w") as f:
        json.dump(existing, f, indent=2, default=str)


def _format_tg_message(result: ApplierResult,
                       ctx: Optional[dict]) -> str:
    """Compose the multi-section daily review message."""
    lines = []
    today = dt.date.today().isoformat()
    lines.append(f"📊 Daily Strategy Review — {today}")
    lines.append("")
    if ctx:
        if "portfolio_summary" in ctx:
            lines.append(f"Portfolio: {ctx['portfolio_summary']}")
        if "macro_read" in ctx:
            lines.append(f"Macro read: {ctx['macro_read']}")
        if "data_gaps" in ctx:
            gaps = ctx["data_gaps"] or []
            lines.append(f"Data gaps: {', '.join(gaps) if gaps else 'none'}")
        if "reasoning_summary" in ctx:
            lines.append("")
            lines.append(f"Summary: {ctx['reasoning_summary']}")
        lines.append("")

    def _fmt_change(c: ProposedChange, idx: int, prop_id: Optional[str] = None) -> list:
        header = f"{idx}. {c.key}: {c.current_value} → {c.proposed_value}"
        if prop_id:
            header = f"[{prop_id}] " + header
        out = [header,
               f"   Why: {c.rationale}",
               f"   Plan: {c.detailed_plan}",
               f"   Effect: {c.expected_effect}",
               f"   Confidence: {c.confidence:.2f}"]
        if prop_id:
            out.append(f"   Approve: /strategy-approve {prop_id}")
            out.append(f"   Reject:  /strategy-reject  {prop_id}")
        return out

    if result.applied_low:
        lines.append("━" * 31)
        lines.append("✅ AUTO-APPLIED (low-risk)")
        for i, c in enumerate(result.applied_low, 1):
            lines.extend(_fmt_change(c, i))
            lines.append("")
    if result.queued_high:
        lines.append("━" * 31)
        lines.append("⏳ NEEDS YOUR APPROVAL (high-risk)")
        for i, c in enumerate(result.queued_high, 1):
            pid = f"prop_{today}_{i:02d}"
            lines.extend(_fmt_change(c, i, prop_id=pid))
            lines.append("")
    if result.rejected_forbidden or result.rejected_out_of_bounds or result.rejected_malformed:
        lines.append("━" * 31)
        lines.append("🚫 REJECTED")
        for c in result.rejected_forbidden:
            lines.append(f"   (forbidden) {c.key}: {c.current_value} → {c.proposed_value}")
        for c in result.rejected_out_of_bounds:
            lines.append(f"   (out of bounds) {c.key}: {c.current_value} → {c.proposed_value}")
        for m in result.rejected_malformed:
            lines.append(f"   (malformed) {m}")

    if not (result.applied_low or result.queued_high
            or result.rejected_forbidden or result.rejected_out_of_bounds):
        lines.append("No changes proposed today.")

    return "\n".join(lines)


def _append_audit_log(result: ApplierResult,
                      ctx: Optional[dict],
                      dry_run: bool) -> None:
    os.makedirs(os.path.dirname(AUDIT_LOG_PATH), exist_ok=True)
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": dry_run,
        "applied_low_count": len(result.applied_low),
        "queued_high_count": len(result.queued_high),
        "rejected_forbidden_count": len(result.rejected_forbidden),
        "rejected_out_of_bounds_count": len(result.rejected_out_of_bounds),
        "rejected_malformed_count": len(result.rejected_malformed),
        "context": ctx or {},
    }
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
```

- [ ] **Step 4: Verify tests pass**

Run: `python3 -m pytest tests/test_quant_applier.py -v`
Expected: all tests PASS (classification + apply path).

- [ ] **Step 5: Commit**

```bash
git add quant/applier.py tests/test_quant_applier.py
git commit -m "feat: quant applier — apply/queue/reject + TG formatter + audit log"
```

---

## Task 11: `scripts/quant_fetch_portfolio.py`

**Files:**
- Create: `scripts/quant_fetch_portfolio.py`
- Create: `tests/test_quant_cli_scripts.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_quant_cli_scripts.py
import json
import subprocess
import sys
import os


def test_quant_fetch_portfolio_outputs_valid_json(monkeypatch, tmp_path):
    """Run the CLI as a subprocess with stubbed Alpaca and verify JSON on stdout."""
    env = {**os.environ, "QUANT_REVIEW_FAKE_BROKER": "1"}
    proc = subprocess.run(
        [sys.executable, "scripts/quant_fetch_portfolio.py"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "cash" in data
    assert "equity" in data
    assert "positions" in data
    assert isinstance(data["positions"], list)
```

- [ ] **Step 2: Verify test fails**

Run: `python3 -m pytest tests/test_quant_cli_scripts.py::test_quant_fetch_portfolio_outputs_valid_json -v`
Expected: FAIL (script doesn't exist yet).

- [ ] **Step 3: Create the script**

```python
#!/usr/bin/env python3
"""Dump current portfolio state as JSON to stdout.

Used by the quant review subagent via Bash. The agent parses stdout directly.
Set QUANT_REVIEW_FAKE_BROKER=1 in the environment to use the in-memory
FakeBroker (for tests / smoke runs without Alpaca credentials)."""
from __future__ import annotations
import json
import os
import sys

# Add the project root to sys.path so we can import project modules regardless
# of cwd when the script is invoked directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import config

def main() -> int:
    if os.environ.get("QUANT_REVIEW_FAKE_BROKER") == "1":
        from tests.fakes import FakeBroker
        broker = FakeBroker(cash=100_000.0, equity=100_000.0)
    else:
        from broker import Broker
        broker = Broker(env=config.ALPACA_ENV)

    from orders import sync_state
    snap = sync_state(broker, alerts=[])

    out = {
        "as_of": snap.synced_at,
        "alpaca_env": snap.alpaca_env,
        "cash": snap.cash,
        "equity": snap.equity,
        "positions": snap.positions,
        "tranches": snap.tranches,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Make executable + verify**

```bash
chmod +x scripts/quant_fetch_portfolio.py
python3 -m pytest tests/test_quant_cli_scripts.py::test_quant_fetch_portfolio_outputs_valid_json -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/quant_fetch_portfolio.py tests/test_quant_cli_scripts.py
git commit -m "feat: scripts/quant_fetch_portfolio.py — CLI for agent's Bash tool"
```

---

## Task 12: `scripts/quant_fetch_externals.py`

**Files:**
- Create: `scripts/quant_fetch_externals.py`
- Modify: `tests/test_quant_cli_scripts.py`

- [ ] **Step 1: Append failing test**

```python
def test_quant_fetch_externals_outputs_five_signals():
    """With all fetchers stubbed via env var, verify the script emits 5 signals."""
    env = {**os.environ, "QUANT_REVIEW_FAKE_EXTERNALS": "1"}
    proc = subprocess.run(
        [sys.executable, "scripts/quant_fetch_externals.py"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "signals" in data
    assert len(data["signals"]) == 5
    sources = {s["source"] for s in data["signals"]}
    assert sources == {"13F", "reddit", "etf-holdings", "ark", "congress"}
```

- [ ] **Step 2: Verify fails; Step 3: Create script**

```python
#!/usr/bin/env python3
"""Run all five external-signal fetchers in parallel and emit combined JSON.

Used by the quant review subagent. Set QUANT_REVIEW_FAKE_EXTERNALS=1 to
return stubbed signals (no network). Otherwise, fetch live."""
from __future__ import annotations
import datetime as dt
import json
import os
import sys
from dataclasses import asdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _fake_signals():
    from quant.schema import ExternalSignal
    now = dt.datetime.now(dt.timezone.utc)
    return [
        ExternalSignal(source=s, as_of=now, data=[{"stub": True}])
        for s in ("13F", "reddit", "etf-holdings", "ark", "congress")
    ]


def main() -> int:
    if os.environ.get("QUANT_REVIEW_FAKE_EXTERNALS") == "1":
        signals = _fake_signals()
    else:
        from quant.data_sources import fetch_all_externals
        signals = fetch_all_externals()

    out = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signals": [
            {
                "source": s.source,
                "as_of": s.as_of.isoformat() if hasattr(s.as_of, "isoformat") else s.as_of,
                "data": s.data,
                "error": s.error,
            }
            for s in signals
        ],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verify + commit**

```bash
chmod +x scripts/quant_fetch_externals.py
python3 -m pytest tests/test_quant_cli_scripts.py -v
git add scripts/quant_fetch_externals.py tests/test_quant_cli_scripts.py
git commit -m "feat: scripts/quant_fetch_externals.py — parallel 5-source CLI"
```

---

## Task 13: `scripts/quant_apply.py`

**Files:**
- Create: `scripts/quant_apply.py`
- Modify: `tests/test_quant_cli_scripts.py`

- [ ] **Step 1: Append failing test**

```python
def test_quant_apply_reads_proposals_and_writes_outputs(tmp_path, monkeypatch):
    proposals = {
        "date": "2026-04-19",
        "portfolio_summary": "ok",
        "macro_read": "risk-on",
        "reasoning_summary": "no big moves",
        "data_gaps": [],
        "proposed_changes": [
            {
                "key": "STOP_LOSS_PCT",
                "current_value": 0.08,
                "proposed_value": 0.075,
                "rationale": "ATR compressed",
                "detailed_plan": "tighter stop next rebalance",
                "expected_effect": "cuts losers faster",
                "risk_tier": "low",
                "confidence": 0.75,
            }
        ],
        "no_changes_reason": None,
    }
    proposals_path = tmp_path / "proposed.json"
    proposals_path.write_text(json.dumps(proposals))

    overrides_path = tmp_path / "overrides.json"
    cache_dir = tmp_path / ".cache"
    cache_dir.mkdir()
    # Script reads paths from applier module; override via env for the cache root
    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_path),
        "QUANT_APPLY_PROPOSALS_PATH": str(tmp_path / "proposals_out.json"),
        "QUANT_APPLY_TG_PATH": str(tmp_path / "tg.json"),
        "QUANT_APPLY_AUDIT_PATH": str(tmp_path / "audit.log"),
    }
    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", str(proposals_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    overrides = json.loads(overrides_path.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075


def test_quant_apply_dry_run_flag(tmp_path, monkeypatch):
    proposals = {
        "date": "2026-04-19", "portfolio_summary": "", "macro_read": "",
        "reasoning_summary": "", "data_gaps": [],
        "proposed_changes": [
            {"key": "STOP_LOSS_PCT", "current_value": 0.08, "proposed_value": 0.075,
             "rationale": "r", "detailed_plan": "p", "expected_effect": "e",
             "risk_tier": "low", "confidence": 0.75}
        ],
        "no_changes_reason": None,
    }
    proposals_path = tmp_path / "proposed.json"
    proposals_path.write_text(json.dumps(proposals))
    overrides_path = tmp_path / "overrides.json"
    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_path),
        "QUANT_APPLY_PROPOSALS_PATH": str(tmp_path / "proposals_out.json"),
        "QUANT_APPLY_TG_PATH": str(tmp_path / "tg.json"),
        "QUANT_APPLY_AUDIT_PATH": str(tmp_path / "audit.log"),
        "QUANT_APPLY_DRY_PATH": str(tmp_path / "dry.json"),
    }
    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", "--dry-run", str(proposals_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    # In dry-run: overrides NOT written, dry artifact IS.
    assert not overrides_path.exists()
    assert (tmp_path / "dry.json").exists()
```

- [ ] **Step 2: Verify tests fail**

- [ ] **Step 3: Create the script**

```python
#!/usr/bin/env python3
"""Apply a QuantReview JSON (the agent's proposed_changes.json) via the
applier. Writes .cache/strategy_overrides.json, .cache/strategy_proposals.json,
.cache/telegram_notifications.json, and .cache/quant_review.log.

Usage:
    python3 scripts/quant_apply.py path/to/proposed_changes.json
    python3 scripts/quant_apply.py --dry-run path/to/proposed_changes.json

Env-var overrides for testing (not normally set):
    QUANT_APPLY_OVERRIDES_PATH
    QUANT_APPLY_PROPOSALS_PATH
    QUANT_APPLY_TG_PATH
    QUANT_APPLY_AUDIT_PATH
    QUANT_APPLY_DRY_PATH
"""
from __future__ import annotations
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposals_file", help="Path to the QuantReview JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write overrides/proposals; write dry artifact instead")
    args = ap.parse_args()

    # Apply any test-only path overrides
    import quant.applier as applier
    for env_name, attr_name in (
        ("QUANT_APPLY_OVERRIDES_PATH", "OVERRIDES_PATH"),
        ("QUANT_APPLY_PROPOSALS_PATH", "PROPOSALS_PATH"),
        ("QUANT_APPLY_TG_PATH", "TG_NOTIFY_PATH"),
        ("QUANT_APPLY_AUDIT_PATH", "AUDIT_LOG_PATH"),
        ("QUANT_APPLY_DRY_PATH", "DRY_RUN_PATH"),
    ):
        if env_name in os.environ:
            setattr(applier, attr_name, os.environ[env_name])

    with open(args.proposals_file) as f:
        review_data = json.load(f)

    from quant.schema import ProposedChange
    raw_changes = review_data.get("proposed_changes", [])
    changes = []
    malformed = []
    for rc in raw_changes:
        try:
            changes.append(ProposedChange(**rc))
        except (TypeError, KeyError) as e:
            malformed.append({"raw": rc, "error": str(e)})

    context = {
        "portfolio_summary": review_data.get("portfolio_summary", ""),
        "macro_read": review_data.get("macro_read", ""),
        "reasoning_summary": review_data.get("reasoning_summary", ""),
        "data_gaps": review_data.get("data_gaps", []),
        "no_changes_reason": review_data.get("no_changes_reason"),
    }

    result = applier.apply(changes, dry_run=args.dry_run, review_context=context)

    # Record any schema-level malformed entries from parse time
    result.rejected_malformed.extend(malformed)

    print(json.dumps({
        "applied_low": len(result.applied_low),
        "queued_high": len(result.queued_high),
        "rejected_forbidden": len(result.rejected_forbidden),
        "rejected_out_of_bounds": len(result.rejected_out_of_bounds),
        "rejected_malformed": len(result.rejected_malformed),
        "dry_run": args.dry_run,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verify + commit**

```bash
chmod +x scripts/quant_apply.py
python3 -m pytest tests/test_quant_cli_scripts.py -v
git add scripts/quant_apply.py tests/test_quant_cli_scripts.py
git commit -m "feat: scripts/quant_apply.py — applier CLI with --dry-run"
```

---

## Task 14: Trigger prompt canonical source

**Files:**
- Create: `quant/trigger_prompt.md`

- [ ] **Step 1: Create the file**

```markdown
# Quant Review Subagent Trigger Prompt

> This is the version-controlled source of the trigger prompt. When you create
> or update the Claude Code remote trigger, paste this content as the prompt.
>
> Update via: `schedule update <trigger-id>` after editing this file.

You are a senior US-equity quant with 15+ years experience in systematic
trading. You are reviewing the day's portfolio close state for a $100K
two-tranche system (core $90K: dual-momentum ETF rotation + value/quality
stock screen + macro overlay; aggressive $10K: leveraged-ETF momentum).

Your job: once per trading day, 3 hours after close, produce a structured
review JSON with zero or more proposed parameter changes.

## WORKFLOW

Run these commands in order using your Bash tool:

1. `python3 /Users/zl/works/stock/scripts/quant_fetch_portfolio.py`
   → parses Alpaca state, returns portfolio JSON

2. `python3 /Users/zl/works/stock/scripts/quant_fetch_externals.py`
   → runs 5 external-signal fetchers in parallel, returns combined JSON

3. Read `/Users/zl/works/stock/.cache/strategy_overrides.json`
   (may not exist on first run — that's fine)
   → your current active overrides; don't re-propose already-applied changes

4. Think step by step. Produce a QuantReview object per the schema below.

5. Write the JSON to `/Users/zl/works/stock/.cache/proposed_changes.json`.

6. `python3 /Users/zl/works/stock/scripts/quant_apply.py /Users/zl/works/stock/.cache/proposed_changes.json`
   → applier classifies, applies/queues/rejects, writes the TG notification

7. Read `.cache/telegram_notifications.json`, `.cache/strategy_overrides.json`,
   and `.cache/strategy_proposals.json`. Confirm state, summarize your final
   decisions in your response (this is the audit trail).

## RULES

- **Default to NO changes.** Only propose when a specific signal justifies it.
  An unsupported change is worse than no change.

- **Low-risk allowlist** (auto-applies if within bounds):
  - `WATCHLIST` — additions only, ≤100 total
  - `NEWS_SHOCK_KEYWORDS` — additions only, ≤30 total
  - `STOP_LOSS_PCT` — within ±20% of current AND in [0.04, 0.20]
  - `TRAILING_STOP_PCT` — within ±20% AND in [0.06, 0.25]
  - `CASH_BUFFER_PCT` — within ±50% AND in [0.02, 0.20]

- **High-risk allowlist** (queues for user approval):
  - `WATCHLIST` removals; `NEWS_SHOCK_KEYWORDS` removals
  - `MOMENTUM_TOP_N`
  - `ETF_ALLOCATION_PCT`; `STOCK_ALLOCATION_PCT`
  - `SCREEN_MIN_ROE`; `SCREEN_MAX_PE`; `SCREEN_MAX_DEBT_EQUITY`
  - `MOMENTUM_LOOKBACK_MONTHS`
  - `SAFE_HAVEN`
  - Out-of-bound changes to the low-risk numeric keys

- **Forbidden (never propose):**
  - `DAILY_MAX_ORDERS`, `DAILY_MAX_NOTIONAL`, `LARGE_ORDER_THRESHOLD`
  - Any `CIRCUIT_BREAKERS` key
  - `EXECUTOR_SHADOW_MODE`
  - `AGGRESSIVE_TRANCHE_PCT`, `INITIAL_CAPITAL`
  - Any path or credential constant
  - `EXECUTOR_WINDOW_START/END`, `EXECUTOR_TICK_MINUTES`
  - `PLANNER_DIRECT_SUBMIT_THRESHOLD`

- **Every proposed change MUST cite** a specific data signal (which of the
  five sources, which datum). Changes without citations are malformed.

- **Acknowledge staleness.** 13F is quarterly-lagged (up to ~45 days after
  quarter close) — treat as positioning context, not current holdings. Flag
  missing or stale data in `data_gaps`.

- **When uncertain, prefer "no changes"** with a clear `no_changes_reason`.

## SCHEMA

Your `proposed_changes.json` must match this shape:

```json
{
  "date": "2026-04-19",
  "portfolio_summary": "brief 1-2 sentence state summary",
  "macro_read": "e.g. 'risk-on at +0.45; VIX benign'",
  "reasoning_summary": "2-3 sentence thesis for today's recommendations",
  "data_gaps": ["list of any stale/missing data sources"],
  "proposed_changes": [
    {
      "key": "STOP_LOSS_PCT",
      "current_value": 0.08,
      "proposed_value": 0.075,
      "rationale": "one paragraph; must cite specific data",
      "detailed_plan": "one paragraph; concrete portfolio effect",
      "expected_effect": "short: e.g. 'cuts losers 15% faster'",
      "risk_tier": "low",
      "confidence": 0.70
    }
  ],
  "no_changes_reason": null
}
```

If `proposed_changes` is empty, `no_changes_reason` must be a non-empty string.

## PHASE 0 DRY-RUN

If this prompt contains the directive `DRY_RUN=True`, run step 6 with the
`--dry-run` flag:

```
python3 /Users/zl/works/stock/scripts/quant_apply.py --dry-run /Users/zl/works/stock/.cache/proposed_changes.json
```

This writes to `.cache/quant_review_dry.json` instead of the live files.

<!-- DRY_RUN=True -->
```

- [ ] **Step 2: Commit**

```bash
git add quant/trigger_prompt.md
git commit -m "docs: canonical quant review trigger prompt

Version-controlled source for the Claude Code remote trigger. When the
trigger is created or updated via the schedule skill, paste this file's
content as the prompt. Keeps prompt diffs visible in git history."
```

---

## Task 15: End-to-end integration test (canned agent output)

**Files:**
- Create: `tests/test_quant_integration.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end test of the quant review pipeline with a canned agent output.

No LLM call; we simulate what the agent would have written to
.cache/proposed_changes.json and verify the full apply → outputs flow."""
import json
import os
import subprocess
import sys
import tempfile


def test_end_to_end_pipeline(tmp_path):
    """Simulate one day's review end-to-end with canned inputs."""
    # Step 1: simulate what the agent produces in proposed_changes.json
    review = {
        "date": "2026-04-19",
        "portfolio_summary": "$100,250 equity; risk-on macro; 10 positions across tranches.",
        "macro_read": "risk-on at +0.45; VIX 17.7",
        "reasoning_summary": (
            "Macro firmly risk-on but 13F shows Q4 tech-trimming; Reddit NVDA "
            "sentiment bearish; Pelosi bought TSLA. Tighten risk controls slightly; "
            "add two accumulating names."
        ),
        "data_gaps": [],
        "proposed_changes": [
            # Low-risk — should auto-apply
            {"key": "STOP_LOSS_PCT", "current_value": 0.08, "proposed_value": 0.075,
             "rationale": "10-day realized ATR compressed 30% — tighter matches vol regime.",
             "detailed_plan": "Next rebalance attaches 7.5% stops instead of 8%.",
             "expected_effect": "~15% earlier cut on losers.",
             "risk_tier": "low", "confidence": 0.70},
            # High-risk — should queue
            {"key": "MOMENTUM_TOP_N", "current_value": 4, "proposed_value": 3,
             "rationale": "Top-1 XLK 3x rank-4 MTUM momentum; concentration helps in trends.",
             "detailed_plan": "Drop MTUM ~$15K, redistribute to rank 1-3.",
             "expected_effect": "top-3 concentration rises 60→75%.",
             "risk_tier": "high", "confidence": 0.65},
            # Forbidden — should reject
            {"key": "DAILY_MAX_NOTIONAL", "current_value": 25_000, "proposed_value": 100_000,
             "rationale": "Larger runway", "detailed_plan": "allow larger orders",
             "expected_effect": "more capacity", "risk_tier": "low", "confidence": 0.9},
        ],
        "no_changes_reason": None,
    }
    proposals_file = tmp_path / "proposed_changes.json"
    proposals_file.write_text(json.dumps(review, indent=2))

    overrides_file = tmp_path / "strategy_overrides.json"
    queue_file = tmp_path / "strategy_proposals.json"
    tg_file = tmp_path / "telegram_notifications.json"
    audit_file = tmp_path / "quant_review.log"

    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_file),
        "QUANT_APPLY_PROPOSALS_PATH": str(queue_file),
        "QUANT_APPLY_TG_PATH": str(tg_file),
        "QUANT_APPLY_AUDIT_PATH": str(audit_file),
    }

    # Step 2: run quant_apply.py
    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", str(proposals_file)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    # Step 3: verify outputs
    # 3a — low-risk written to overrides
    overrides = json.loads(overrides_file.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075

    # 3b — high-risk queued with id + expiry
    queue = json.loads(queue_file.read_text())
    assert len(queue) == 1
    assert queue[0]["key"] == "MOMENTUM_TOP_N"
    assert queue[0]["id"].startswith("prop_")
    assert "expires_at" in queue[0]

    # 3c — TG notification contains all three sections
    notifs = json.loads(tg_file.read_text())
    msg = notifs[-1]["message"]
    assert "AUTO-APPLIED" in msg
    assert "STOP_LOSS_PCT" in msg
    assert "NEEDS YOUR APPROVAL" in msg
    assert "MOMENTUM_TOP_N" in msg
    assert "REJECTED" in msg
    assert "DAILY_MAX_NOTIONAL" in msg

    # 3d — audit log has one record
    assert audit_file.exists()
    lines = audit_file.read_text().strip().split("\n")
    record = json.loads(lines[-1])
    assert record["applied_low_count"] == 1
    assert record["queued_high_count"] == 1
    assert record["rejected_forbidden_count"] == 1
```

- [ ] **Step 2: Run**

Run: `python3 -m pytest tests/test_quant_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_quant_integration.py
git commit -m "test: end-to-end integration for quant review pipeline

Canned agent output → quant_apply.py → verify overrides + queue + TG + audit
all written correctly. Covers low-risk apply, high-risk queue, and
forbidden-reject paths in one integrated test."
```

---

## Task 16: README ops docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add "Quant Review Subagent" section to README**

Locate the "## Intraday Execution Layer" section in `README.md`. Insert a new section AFTER it:

```markdown
## Quant Review Subagent

A daily LLM-driven strategy reviewer that runs via a Claude Code scheduled
remote trigger 3 hours after US-market close. Reviews portfolio state against
five external positioning signals, proposes parameter changes within a
risk-tiered allowlist, and reports everything via Telegram.

### Architecture

Single Claude Code remote trigger runs the workflow end-to-end:

1. `scripts/quant_fetch_portfolio.py` — dumps current portfolio state
2. `scripts/quant_fetch_externals.py` — fetches 5 external signals in parallel
   (13F filings, Reddit trending, popular ETF holdings, ARK daily trades,
   Congress/Pelosi STOCK Act disclosures)
3. Agent reasons, produces `.cache/proposed_changes.json`
4. `scripts/quant_apply.py` — classifies per risk tier, writes overrides/queue
   /Telegram notification/audit log

Low-risk changes (small stop-loss tweaks, watchlist additions) auto-apply.
High-risk changes (concentration shifts, screener filter changes) queue in
`.cache/strategy_proposals.json` for Telegram approval. Forbidden keys
(safety rails, credentials) are hard-rejected at two independent layers
(applier + `config.py` override loader).

### Setup

The trigger is created once via Claude Code's `schedule` skill:

```
/schedule create
```

Use the content of `quant/trigger_prompt.md` as the trigger prompt. Cron
schedule: `0 7 * * 2-6` (7 AM local Tue-Sat = 7 PM ET Mon-Fri, market
close + 3h). No `ANTHROPIC_API_KEY` needed — uses your CC subscription.

### Phased rollout

1. **Phase 0 — Dry-run** (~1 week). Trigger prompt includes
   `DRY_RUN=True`. Agent calls `quant_apply.py --dry-run` which writes
   `.cache/quant_review_dry.json` instead of the live files. TG report
   still sends. Review daily.
2. **Phase 1 — Live** (~2-4 weeks). Remove `DRY_RUN` from trigger prompt.
   Low-risk auto-applies; high-risk queues. Approve via direct JSON edit
   or (if bot ready) TG commands.
3. **Phase 2 — TG bot approval handlers** (separate repo).

### Files

| File | Purpose |
|---|---|
| `.cache/strategy_overrides.json` | Active overrides; read by `config.py` at module-load time |
| `.cache/strategy_proposals.json` | Pending high-risk queue; written by applier, consumed by TG bot |
| `.cache/telegram_notifications.json` | TG message queue (shared with executor-breaker notifications) |
| `.cache/proposed_changes.json` | Agent's intermediate output |
| `.cache/quant_review_dry.json` | Phase-0 dry-run artifact |
| `.cache/quant_review.log` | Append-only audit log |
| `quant/trigger_prompt.md` | Canonical version-controlled trigger prompt |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README — Quant Review Subagent section"
```

---

## Self-Review

After completing all tasks, verify:

- [ ] **Spec coverage:**
  - §3 Architecture → Tasks 11, 12, 13, 14
  - §4 Data sources → Tasks 3, 4, 5, 6, 7, 8
  - §5 Override mechanism + risk tiers → Tasks 1, 9
  - §6 Schema + prompt + TG report → Tasks 2, 10, 14
  - §7 Error handling → Tasks 3-8 (per-fetcher), 9-10 (applier rejections), 1 (config loader)
  - §8 Testing → Each task's tests + Task 15 integration
  - §9 Rollout → Task 14 DRY_RUN flag + Task 16 README phases

- [ ] **Placeholder scan:** none of "TBD", "TODO", "implement later", "similar to task N" without code — all tasks have explicit code.

- [ ] **Type consistency:**
  - `ExternalSignal` fields (`source`, `as_of`, `data`, `error`) consistent across Tasks 2-8.
  - `ProposedChange` fields consistent across Tasks 2, 9, 10, 13, 14, 15.
  - Applier path constants (`OVERRIDES_PATH`, `PROPOSALS_PATH`, etc.) match in Task 10, 13, and the integration test in Task 15.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-19-quant-review-subagent.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
