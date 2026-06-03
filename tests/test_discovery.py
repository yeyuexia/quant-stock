"""Tests for the refactored discovery.py — focused on the correctness
bugs that motivated the rewrite (determinism, fail-closed, rank-based
scoring, US-only, smart-money harvesting)."""
import os
import sys
import json
import tempfile
import pandas as pd
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import discovery
import config


# ── merge_candidates: determinism + priority order ────────────────

def test_merge_candidates_is_deterministic(monkeypatch):
    """Two consecutive calls with identical inputs must return identical lists."""
    monkeypatch.setattr(discovery, "get_smart_money_tickers",
                        lambda *a, **kw: {"NVDA": ["13F"], "PLTR": ["ark"]})
    monkeypatch.setattr(discovery, "sp500_round_robin_slice",
                        lambda n: ["A", "B", "C", "D"])
    monkeypatch.setattr(config, "WATCHLIST", ["AAPL", "MSFT"])
    a, _ = discovery.merge_candidates(max_scan=20)
    b, _ = discovery.merge_candidates(max_scan=20)
    assert a == b


def test_merge_candidates_priority_order(monkeypatch):
    """Order must be: watchlist → smart-money → reddit → sp500."""
    monkeypatch.setattr(discovery, "get_smart_money_tickers",
                        lambda *a, **kw: {"SMRT1": ["13F"], "SMRT2": ["ark"]})
    monkeypatch.setattr(discovery, "get_reddit_trending_tickers",
                        lambda *a, **kw: ["REDD1"])
    monkeypatch.setattr(discovery, "sp500_round_robin_slice",
                        lambda n: ["SPX1", "SPX2"])
    monkeypatch.setattr(config, "WATCHLIST", ["WL1", "WL2"])
    ordered, _ = discovery.merge_candidates(include_reddit=True, max_scan=20)
    assert ordered[:2] == ["WL1", "WL2"]
    # Smart-money set order varies internally but both must precede reddit/sp500
    assert {ordered[2], ordered[3]} == {"SMRT1", "SMRT2"}
    assert ordered[4] == "REDD1"
    assert ordered[5:7] == ["SPX1", "SPX2"]


def test_merge_candidates_dedupes_across_sources(monkeypatch):
    """A ticker present in multiple feeds appears once, with merged source list."""
    monkeypatch.setattr(discovery, "get_smart_money_tickers",
                        lambda *a, **kw: {"AAPL": ["13F", "etf-holdings"]})
    monkeypatch.setattr(discovery, "sp500_round_robin_slice",
                        lambda n: ["AAPL", "NVDA"])
    monkeypatch.setattr(config, "WATCHLIST", ["AAPL"])
    ordered, sources = discovery.merge_candidates(max_scan=20)
    assert ordered.count("AAPL") == 1
    assert "watchlist" in sources["AAPL"]
    assert "13F" in sources["AAPL"]
    assert "sp500" in sources["AAPL"]


def test_merge_candidates_includes_full_sp500(monkeypatch):
    """With production constants, one run must scan the ENTIRE S&P 500 — never a
    truncated slice. The cap must leave room for the full universe on top of the
    watchlist + a substantial smart-money set (regression: DISCOVERY_MAX_SCAN was
    200 / DISCOVERY_SP500_BATCH 50, so a name like MRVL could be skipped run after
    run)."""
    universe = [f"SP{i}" for i in range(503)]
    # Real round-robin returns the full universe when batch >= len(universe); the
    # doubled list models the wrap so any batch >= 503 yields every name.
    monkeypatch.setattr(discovery, "sp500_round_robin_slice",
                        lambda n: (universe * 2)[:n])
    monkeypatch.setattr(discovery, "get_smart_money_tickers",
                        lambda *a, **kw: {f"SM{i}": ["13F"] for i in range(150)})
    monkeypatch.setattr(config, "WATCHLIST", [f"WL{i}" for i in range(51)])
    ordered, _ = discovery.merge_candidates()  # uses config production constants
    assert set(universe).issubset(set(ordered)), \
        "every S&P 500 name must be scanned in a single discovery run"


def test_merge_candidates_respects_max_scan(monkeypatch):
    monkeypatch.setattr(discovery, "get_smart_money_tickers", lambda *a, **kw: {})
    monkeypatch.setattr(discovery, "sp500_round_robin_slice", lambda n: [f"S{i}" for i in range(n)])
    monkeypatch.setattr(config, "WATCHLIST", ["A", "B", "C"])
    ordered, _ = discovery.merge_candidates(max_scan=5)
    assert len(ordered) == 5
    assert ordered[:3] == ["A", "B", "C"]


# ── S&P 500 round-robin pointer ───────────────────────────────────

def test_sp500_round_robin_advances_and_wraps(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "get_sp500_tickers",
                        lambda: ["T1", "T2", "T3", "T4", "T5"])
    monkeypatch.setattr(discovery, "SP500_POINTER", str(tmp_path / "ptr.json"))
    first = discovery.sp500_round_robin_slice(2)
    second = discovery.sp500_round_robin_slice(2)
    third = discovery.sp500_round_robin_slice(2)  # wraps: T5, T1
    assert first == ["T1", "T2"]
    assert second == ["T3", "T4"]
    assert third == ["T5", "T1"]


def test_sp500_round_robin_no_universe(monkeypatch):
    monkeypatch.setattr(discovery, "get_sp500_tickers", lambda: [])
    assert discovery.sp500_round_robin_slice(10) == []


# ── passes_criteria: fail-closed for value/quality ────────────────

def test_passes_criteria_fail_closed_on_missing_pe():
    """A loss-making (or unknown-PE) stock must NOT pass a value filter."""
    stock = {"market_cap": 5e9, "pe": None, "roe": 0.20}
    criteria = {"max_pe": 25, "min_market_cap": 1e9}
    assert discovery.passes_criteria(stock, criteria) is False


def test_passes_criteria_fail_closed_on_negative_pe():
    """Negative P/E (loss-maker) fails the value gate."""
    stock = {"market_cap": 5e9, "pe": -10, "roe": 0.20}
    criteria = {"max_pe": 25, "min_market_cap": 1e9}
    assert discovery.passes_criteria(stock, criteria) is False


def test_passes_criteria_fail_open_on_missing_growth():
    """Missing growth is fail-open (downstream CANSLIM re-checks)."""
    stock = {"market_cap": 5e9, "rev_growth": None}
    criteria = {"min_rev_growth": 0.10, "min_market_cap": 1e9}
    assert discovery.passes_criteria(stock, criteria) is True


def test_passes_criteria_market_cap_strict():
    stock = {"market_cap": 100e6}  # too small
    criteria = {"min_market_cap": 1e9}
    assert discovery.passes_criteria(stock, criteria) is False


# ── compute_composite_scores: rank-based, not absolute ────────────

def test_composite_score_is_rank_based(monkeypatch):
    """Doubling every stock's rev_growth must not change relative ranks."""
    base = pd.DataFrame([
        {"ticker": "A", "rs_pct": 90, "rev_growth": 0.50, "eps_q_growth": 0.40,
         "roe": 0.30, "ret_3m": 0.20, "dist_52w_high": -0.05, "ipo_age_years": 2,
         "sma50_dist_pct": 0.10, "pe": 30, "quarterly_eps": []},
        {"ticker": "B", "rs_pct": 50, "rev_growth": 0.10, "eps_q_growth": 0.05,
         "roe": 0.10, "ret_3m": 0.00, "dist_52w_high": -0.20, "ipo_age_years": 15,
         "sma50_dist_pct": -0.05, "pe": 18, "quarterly_eps": []},
        {"ticker": "C", "rs_pct": 30, "rev_growth": 0.05, "eps_q_growth": -0.10,
         "roe": 0.05, "ret_3m": -0.05, "dist_52w_high": -0.30, "ipo_age_years": 30,
         "sma50_dist_pct": -0.15, "pe": 50, "quarterly_eps": []},
    ])
    scored_a = discovery.compute_composite_scores(base.copy())

    # Scale rev_growth 100x — ranks unchanged, ordering should be identical
    base2 = base.copy()
    base2["rev_growth"] = base2["rev_growth"] * 100
    scored_b = discovery.compute_composite_scores(base2)
    assert scored_a["composite_score"].rank().tolist() == \
           scored_b["composite_score"].rank().tolist()


def test_composite_score_excludes_negative_pe_from_value():
    """A loss-making name shouldn't get a high value-PE rank just because PE is huge."""
    df = pd.DataFrame([
        {"ticker": "PROFITABLE", "rs_pct": 50, "rev_growth": 0.10, "eps_q_growth": 0.05,
         "roe": 0.10, "ret_3m": 0.0, "dist_52w_high": -0.10, "ipo_age_years": 5,
         "sma50_dist_pct": 0.0, "pe": 15, "quarterly_eps": []},
        {"ticker": "LOSSMAKER", "rs_pct": 50, "rev_growth": 0.10, "eps_q_growth": 0.05,
         "roe": 0.10, "ret_3m": 0.0, "dist_52w_high": -0.10, "ipo_age_years": 5,
         "sma50_dist_pct": 0.0, "pe": -50, "quarterly_eps": []},
    ])
    scored = discovery.compute_composite_scores(df)
    # value_pe rank for the loss-maker should be the NaN-fallback (50), strictly
    # less than the profitable name's 100.
    assert scored.loc[scored.ticker == "LOSSMAKER", "rank_value_pe"].iloc[0] \
        < scored.loc[scored.ticker == "PROFITABLE", "rank_value_pe"].iloc[0]


def test_composite_score_uses_config_weights(monkeypatch):
    """Custom weights must change the ordering as expected."""
    df = pd.DataFrame([
        {"ticker": "X", "rs_pct": 90, "rev_growth": 0.05, "eps_q_growth": 0.05,
         "roe": 0.05, "ret_3m": 0.0, "dist_52w_high": -0.10, "ipo_age_years": 5,
         "sma50_dist_pct": 0.0, "pe": 25, "quarterly_eps": []},
        {"ticker": "Y", "rs_pct": 10, "rev_growth": 0.95, "eps_q_growth": 0.05,
         "roe": 0.05, "ret_3m": 0.0, "dist_52w_high": -0.10, "ipo_age_years": 5,
         "sma50_dist_pct": 0.0, "pe": 25, "quarterly_eps": []},
    ])
    # All weight on RS → X wins
    weights_rs_only = {k: (1.0 if k == "rs" else 0.0) for k in config.DISCOVERY_WEIGHTS}
    a = discovery.compute_composite_scores(df.copy(), weights_rs_only)
    assert a.iloc[a["composite_score"].idxmax()]["ticker"] == "X"

    # All weight on rev_growth → Y wins
    weights_rev_only = {k: (1.0 if k == "rev_growth" else 0.0) for k in config.DISCOVERY_WEIGHTS}
    b = discovery.compute_composite_scores(df.copy(), weights_rev_only)
    assert b.iloc[b["composite_score"].idxmax()]["ticker"] == "Y"


# ── EPS acceleration ──────────────────────────────────────────────

def test_eps_acceleration_score_basic():
    # q0=1.5, q1=1.0, q2=0.7  →  g1=0.5, g2=0.43, accelerating
    assert discovery._eps_acceleration_score([1.5, 1.0, 0.7]) > 0
    # Deceleration
    assert discovery._eps_acceleration_score([1.1, 1.0, 0.8]) == 0.0
    # Too few quarters
    assert discovery._eps_acceleration_score([1.0]) == 0.0
    # Zero denominators
    assert discovery._eps_acceleration_score([1.0, 0.0, 0.5]) == 0.0


# ── US-only filter ────────────────────────────────────────────────

def test_fetch_ticker_snapshot_rejects_non_us(monkeypatch):
    """A Chinese ADR (country='China') is rejected when DISCOVERY_REQUIRE_US=True."""
    monkeypatch.setattr(config, "DISCOVERY_REQUIRE_US", True)
    fake_info = {"quoteType": "EQUITY", "marketCap": 50e9, "country": "China",
                 "currentPrice": 100}
    monkeypatch.setattr(discovery.data_mod, "fetch_info", lambda t: fake_info)
    monkeypatch.setattr(discovery.data_mod, "fetch_fundamentals", lambda t: {})
    assert discovery.fetch_ticker_snapshot("BABA") is None


def test_fetch_ticker_snapshot_accepts_us(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_REQUIRE_US", True)
    fake_info = {"quoteType": "EQUITY", "marketCap": 50e9,
                 "country": "United States", "currentPrice": 100,
                 "shortName": "Test Corp"}
    monkeypatch.setattr(discovery.data_mod, "fetch_info", lambda t: fake_info)
    monkeypatch.setattr(discovery.data_mod, "fetch_fundamentals", lambda t: {})
    snap = discovery.fetch_ticker_snapshot("AAPL")
    assert snap is not None
    assert snap["ticker"] == "AAPL"
    assert snap["country"] == "United States"


def test_fetch_ticker_snapshot_rejects_micro_cap(monkeypatch):
    fake_info = {"quoteType": "EQUITY", "marketCap": 50e6,
                 "country": "United States"}
    monkeypatch.setattr(discovery.data_mod, "fetch_info", lambda t: fake_info)
    assert discovery.fetch_ticker_snapshot("MICRO") is None


def test_fetch_ticker_snapshot_rejects_non_equity(monkeypatch):
    fake_info = {"quoteType": "ETF", "marketCap": 100e9}
    monkeypatch.setattr(discovery.data_mod, "fetch_info", lambda t: fake_info)
    assert discovery.fetch_ticker_snapshot("SPY") is None


# ── Smart-money ticker harvesting ────────────────────────────────

def test_get_smart_money_tickers_extracts_from_signals(monkeypatch):
    """Verifies ExternalSignal data rows yield uppercase tickers across sources."""
    from quant.schema import ExternalSignal
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    fake_signals = [
        ExternalSignal(source="13F", as_of=now,
                       data=[{"ticker": "nvda", "fund": "Berkshire"},
                             {"ticker": "AAPL", "fund": "Berkshire"}]),
        ExternalSignal(source="ark", as_of=now,
                       data=[{"ticker": "PLTR", "fund": "ARKK"}]),
        ExternalSignal(source="reddit", as_of=now,
                       data=[{"ticker": "GME"}]),  # excluded by default
        ExternalSignal(source="congress", as_of=now,
                       data=[{"ticker": "MSFT"}]),
        ExternalSignal(source="etf-holdings", as_of=now,
                       data=[], error="fetch failed"),  # errors skipped
    ]
    monkeypatch.setattr("quant.data_sources.fetch_all_externals",
                        lambda: fake_signals)
    out = discovery.get_smart_money_tickers()
    assert set(out.keys()) == {"NVDA", "AAPL", "PLTR", "MSFT"}
    assert "GME" not in out  # reddit not in default sources
    assert "13F" in out["NVDA"]


def test_get_smart_money_tickers_filters_garbage(monkeypatch):
    """Non-alphabetic or too-long ticker strings are dropped."""
    from quant.schema import ExternalSignal
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    fake_signals = [
        ExternalSignal(source="13F", as_of=now, data=[
            {"ticker": "TOOLONG", "fund": "x"},  # > 5 chars
            {"ticker": "123", "fund": "x"},      # numeric
            {"ticker": "", "fund": "x"},          # empty
            {"ticker": "OK", "fund": "x"},
        ]),
    ]
    monkeypatch.setattr("quant.data_sources.fetch_all_externals", lambda: fake_signals)
    out = discovery.get_smart_money_tickers()
    assert set(out.keys()) == {"OK"}


# ── Pruning ──────────────────────────────────────────────────────

def test_find_stale_watchlist(monkeypatch, tmp_path):
    import datetime as _dt
    monkeypatch.setattr(discovery, "LASTPASS_PATH", str(tmp_path / "lp.json"))
    monkeypatch.setattr(config, "WATCHLIST", ["FRESH", "STALE", "UNKNOWN"])
    monkeypatch.setattr(config, "DISCOVERY_STALE_DAYS", 30)
    today = _dt.date.today()
    fresh = (today - _dt.timedelta(days=5)).isoformat()
    stale = (today - _dt.timedelta(days=60)).isoformat()
    discovery._save_lastpass({"FRESH": fresh, "STALE": stale})
    result = discovery.find_stale_watchlist()
    out_map = dict(result)
    assert "FRESH" not in out_map
    assert "STALE" in out_map and out_map["STALE"] >= 30
    assert "UNKNOWN" in out_map and out_map["UNKNOWN"] is None


def test_record_screener_pass_stamps_today(monkeypatch, tmp_path):
    import datetime as _dt
    monkeypatch.setattr(discovery, "LASTPASS_PATH", str(tmp_path / "lp.json"))
    discovery.record_screener_pass(["AAPL", "NVDA"])
    lp = discovery._load_lastpass()
    today = _dt.date.today().isoformat()
    assert lp["AAPL"] == today
    assert lp["NVDA"] == today


# ── Audit log ────────────────────────────────────────────────────

# ── watchlist_auto.json: generated auto-discovery file ───────────
# discovery --update / --prune now operate on config.WATCHLIST_AUTO_PATH
# (a generated JSON file), NEVER on config.py. The hand-curated WATCHLIST
# block in config.py is the SEED and is never mutated by discovery.

@pytest.fixture
def auto_path(tmp_path, monkeypatch):
    """Redirect the generated auto-watchlist file into tmp_path."""
    p = tmp_path / "watchlist_auto.json"
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(p))
    return p


def test_config_unions_seed_and_auto(tmp_path, monkeypatch):
    """config.WATCHLIST = seed (config.py literal) ∪ auto file (deduped, order)."""
    import importlib
    auto = tmp_path / "watchlist_auto.json"
    auto.write_text(json.dumps(["AAPL", "WMT", "ZNGA"]))  # AAPL/WMT are seed dupes
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(auto))
    reloaded = config._load_auto_watchlist()
    assert reloaded == ["AAPL", "WMT", "ZNGA"]
    union = config._union_watchlist(config.WATCHLIST_SEED, reloaded)
    # seed first, then auto-only names, deduped, order preserved
    assert union[: len(config.WATCHLIST_SEED)] == config.WATCHLIST_SEED
    assert "ZNGA" in union
    assert union.count("AAPL") == 1
    assert union.count("WMT") == 1


def test_config_fails_open_on_missing_auto_file(tmp_path, monkeypatch):
    """Missing watchlist_auto.json → seed only, no crash."""
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(tmp_path / "nope.json"))
    assert config._load_auto_watchlist() == []
    union = config._union_watchlist(config.WATCHLIST_SEED, config._load_auto_watchlist())
    assert union == config.WATCHLIST_SEED


def test_config_fails_open_on_corrupt_auto_file(tmp_path, monkeypatch):
    """Corrupt JSON → seed only, no crash."""
    bad = tmp_path / "watchlist_auto.json"
    bad.write_text("{not valid json[[[")
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(bad))
    assert config._load_auto_watchlist() == []


def test_config_auto_file_filters_invalid_entries(tmp_path, monkeypatch):
    """Only non-empty alpha (dotted/dashed) strings of len<=5 are accepted."""
    auto = tmp_path / "watchlist_auto.json"
    auto.write_text(json.dumps(
        ["GOOD", "BRK-B", "123", "TOOLONG", "", "  ", 42, None, "OK"]
    ))
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(auto))
    assert config._load_auto_watchlist() == ["GOOD", "BRK-B", "OK"]


def test_config_watchlist_override_length_guard_still_holds():
    """The override-allowlist length bounds on WATCHLIST are unchanged."""
    assert config._OVERRIDE_SCHEMA["WATCHLIST"][1] == 1
    assert config._OVERRIDE_SCHEMA["WATCHLIST"][2] == 200


# ── --update: append to watchlist_auto.json, never config.py ──────

def test_update_appends_to_auto_file(auto_path):
    """update_config_watchlist writes new tickers to watchlist_auto.json."""
    combined = discovery.update_config_watchlist(["NEWA", "NEWB"])
    assert "NEWA" in combined and "NEWB" in combined
    assert auto_path.exists()
    stored = json.loads(auto_path.read_text())
    assert stored == ["NEWA", "NEWB"]


def test_update_is_append_only_and_deduped(auto_path):
    """Second update appends without duplicating or reordering existing entries."""
    discovery.update_config_watchlist(["NEWA", "NEWB"])
    discovery.update_config_watchlist(["NEWB", "NEWC"])  # NEWB already present
    stored = json.loads(auto_path.read_text())
    assert stored == ["NEWA", "NEWB", "NEWC"]


def test_update_does_not_touch_config_py(auto_path):
    """--update must never rewrite config.py (seed comments preserved)."""
    config_path = os.path.join(os.path.dirname(config.__file__), "config.py")
    before = open(config_path).read()
    discovery.update_config_watchlist(["NEWA", "NEWB"])
    after = open(config_path).read()
    assert before == after
    # The hand-curated comment must still be present.
    assert "# Mega-cap tech" in after


def test_update_combined_includes_seed(auto_path, monkeypatch):
    """Returned `combined` is seed ∪ auto so existing callers' counts are sane."""
    monkeypatch.setattr(config, "WATCHLIST_SEED", ["SEED1", "SEED2"])
    combined = discovery.update_config_watchlist(["NEWA"])
    assert combined[:2] == ["SEED1", "SEED2"]
    assert "NEWA" in combined


# ── --prune: remove only auto entries; protect seed ──────────────

def test_prune_removes_only_auto_entries(auto_path, monkeypatch):
    """prune_stale removes stale names from the auto file, preserving order."""
    auto_path.write_text(json.dumps(["AUTOA", "AUTOB", "AUTOC"]))
    kept, removed, seed_skipped = discovery.prune_stale_from_config(["AUTOB"])
    assert kept == ["AUTOA", "AUTOC"]
    assert removed == ["AUTOB"]
    assert seed_skipped == []
    assert json.loads(auto_path.read_text()) == ["AUTOA", "AUTOC"]


def test_prune_protects_seed_names(auto_path, monkeypatch):
    """A stale name that's a SEED (not in the auto file) is skipped, not removed."""
    monkeypatch.setattr(config, "WATCHLIST_SEED", ["SEEDA", "SEEDB"])
    auto_path.write_text(json.dumps(["AUTOA"]))
    # SEEDA is seed-only; AUTOA is in the auto file
    kept, removed, seed_skipped = discovery.prune_stale_from_config(["SEEDA", "AUTOA"])
    assert removed == ["AUTOA"]
    assert seed_skipped == ["SEEDA"]
    assert json.loads(auto_path.read_text()) == []


def test_prune_does_not_touch_config_py(auto_path):
    """--prune must never rewrite config.py."""
    auto_path.write_text(json.dumps(["AUTOA", "AUTOB"]))
    config_path = os.path.join(os.path.dirname(config.__file__), "config.py")
    before = open(config_path).read()
    discovery.prune_stale_from_config(["AUTOA"])
    after = open(config_path).read()
    assert before == after


def test_prune_noop_when_ticker_not_present(auto_path):
    auto_path.write_text(json.dumps(["AUTOA", "AUTOB"]))
    kept, removed, seed_skipped = discovery.prune_stale_from_config(["ZZZ"])
    assert kept == ["AUTOA", "AUTOB"]
    assert removed == []
    # ZZZ is neither seed nor auto → reported as seed-skipped is wrong; it's
    # simply absent. It must NOT appear in removed.
    assert "ZZZ" not in removed


def test_prune_main_confirm_skips_never_seen(monkeypatch, tmp_path):
    """--prune --confirm removes explicit-stale AUTO names only; 'never seen'
    entries are skipped and a stale SEED name is left in place."""
    import datetime as _dt
    auto = tmp_path / "watchlist_auto.json"
    auto.write_text(json.dumps(["SAUTO", "NEWB"]))  # SAUTO = stale auto name
    monkeypatch.setattr(config, "WATCHLIST_AUTO_PATH", str(auto))
    monkeypatch.setattr(discovery, "LASTPASS_PATH", str(tmp_path / "lp.json"))
    # Seed has a stale name too; it must be protected.
    monkeypatch.setattr(config, "WATCHLIST_SEED", ["SSEED", "FRESH"])
    monkeypatch.setattr(config, "WATCHLIST",
                        ["SSEED", "FRESH", "SAUTO", "NEWB"])
    monkeypatch.setattr(config, "DISCOVERY_STALE_DAYS", 30)

    today = _dt.date.today()
    discovery._save_lastpass({
        "FRESH": (today - _dt.timedelta(days=5)).isoformat(),
        "SSEED": (today - _dt.timedelta(days=120)).isoformat(),
        "SAUTO": (today - _dt.timedelta(days=120)).isoformat(),
        # NEWB absent → never seen → not auto-pruned
    })

    config_path = os.path.join(os.path.dirname(config.__file__), "config.py")
    cfg_before = open(config_path).read()

    monkeypatch.setattr(sys, "argv", ["discovery.py", "--prune", "--confirm"])
    rc = discovery.main()
    assert rc == 0

    # config.py untouched
    assert open(config_path).read() == cfg_before
    # auto file: SAUTO removed; NEWB kept (never seen); SSEED never
    # was in the auto file so it stays out of it (and was protected as seed).
    stored = json.loads(auto.read_text())
    assert "SAUTO" not in stored
    assert "NEWB" in stored


# ── screener hook wiring ─────────────────────────────────────────

def test_screener_invokes_record_screener_pass(monkeypatch):
    """screen_stocks() must stamp every ticker that passed the technical hard
    gates (ADR + EMA + RS) — not just the final top-N."""
    import screener as sc
    import pandas as pd

    # Build a fake post-filter df via the screener internals: easiest is to
    # patch screen_stocks's inputs so it walks the full path.
    # Override the hook directly with a recording mock for this test only.
    calls = []
    monkeypatch.setattr(sc, "record_screener_pass", lambda ts: calls.append(list(ts)))

    # Stub the heavy data fetches with deterministic shapes.
    n = 252
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    # Monotonically rising series — every ticker comfortably above any EMA.
    base = 100 * np.cumprod(1 + np.full(n, 0.002))
    closes_df = pd.DataFrame({
        "PASS1": base,
        "PASS2": base * 1.1,
        "DROP":  base * 0.9,
    }, index=idx)

    # OHLCV with wide-range bars so ADR clears 0.04
    ohlcv_cols = {}
    for t in closes_df.columns:
        ohlcv_cols[("Close", t)] = closes_df[t]
        ohlcv_cols[("High", t)] = closes_df[t] * 1.05
        ohlcv_cols[("Low", t)] = closes_df[t] * 0.95
        ohlcv_cols[("Open", t)] = closes_df[t]
        ohlcv_cols[("Volume", t)] = pd.Series(2e6, index=idx)
    ohlcv = pd.DataFrame(ohlcv_cols, index=idx)
    ohlcv.columns = pd.MultiIndex.from_tuples(ohlcv.columns)

    monkeypatch.setattr(sc, "fetch_ohlcv", lambda *a, **kw: ohlcv)
    monkeypatch.setattr(sc, "fetch_prices", lambda *a, **kw: closes_df)
    monkeypatch.setattr(sc, "WATCHLIST", ["PASS1", "PASS2", "DROP"])
    monkeypatch.setattr(sc, "SCREEN_RS_MIN", 0)         # let everyone pass RS
    monkeypatch.setattr(sc, "SCREEN_ADR_MIN", 0.01)     # 1% — generous
    monkeypatch.setattr(sc, "SCREEN_EMA_FAST", 5)
    monkeypatch.setattr(sc, "SCREEN_EMA_SLOW", 10)

    sc.screen_stocks()

    # All 3 tickers should have been stamped (passed the technical gates).
    assert len(calls) == 1
    assert set(calls[0]) == {"PASS1", "PASS2", "DROP"}


def test_screener_hook_swallows_exception(monkeypatch):
    """If record_screener_pass raises, screen_stocks must still complete."""
    import screener as sc
    def _boom(_):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sc, "record_screener_pass", _boom)

    # Minimal inputs so screen_stocks runs end-to-end.
    n = 252
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(1)
    closes_df = pd.DataFrame({"X": 100 * np.cumprod(1 + rng.normal(0.001, 0.015, n))}, index=idx)
    ohlcv_cols = {
        ("Close", "X"): closes_df["X"], ("High", "X"): closes_df["X"] * 1.05,
        ("Low", "X"): closes_df["X"] * 0.95, ("Open", "X"): closes_df["X"],
        ("Volume", "X"): pd.Series(2e6, index=idx),
    }
    ohlcv = pd.DataFrame(ohlcv_cols, index=idx)
    ohlcv.columns = pd.MultiIndex.from_tuples(ohlcv.columns)
    monkeypatch.setattr(sc, "fetch_ohlcv", lambda *a, **kw: ohlcv)
    monkeypatch.setattr(sc, "fetch_prices", lambda *a, **kw: closes_df)
    monkeypatch.setattr(sc, "WATCHLIST", ["X"])
    monkeypatch.setattr(sc, "SCREEN_RS_MIN", 0)
    monkeypatch.setattr(sc, "SCREEN_ADR_MIN", 0.01)
    monkeypatch.setattr(sc, "SCREEN_EMA_FAST", 5)
    monkeypatch.setattr(sc, "SCREEN_EMA_SLOW", 10)

    # Should not raise.
    sc.screen_stocks()


def test_log_run_appends_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(discovery, "DISCOVERY_LOG", str(tmp_path / "d.log"))
    df = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "composite_score": [80.0, 60.0, 40.0],
    })
    discovery._log_run(df, {"A": ["x"], "B": ["y"], "C": ["z"]}, mode="scan")
    discovery._log_run(df, {"A": ["x"]}, mode="update")
    with open(tmp_path / "d.log") as f:
        lines = f.readlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    assert rec1["mode"] == "scan"
    assert rec1["candidates"] == 3
    assert rec1["valid"] == 3
    assert rec1["top_10"] == ["A", "B", "C"]


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
