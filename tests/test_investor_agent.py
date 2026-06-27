"""Regression tests for investor_agent.py — claude CLI invocation hardening."""
import logging
import os
import subprocess
import sys
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.agent.investor as investor_agent


def _df():
    return pd.DataFrame([{
        "rank": 1, "ticker": "NVDA", "price": 500.0, "rs_score": 92.0,
        "adr": 0.04, "vcp_contractions": 2, "in_base": True,
        "vol_contracting": True, "vcp_pivot": 510.0,
        "eps_q_growth": 0.45, "rev_growth": 0.30, "eps_accel": True,
    }])


def test_returns_none_on_empty_df():
    assert investor_agent.run_investor_review(pd.DataFrame()) is None


def test_returns_none_when_claude_cli_missing(monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with caplog.at_level(logging.WARNING, logger="investor_agent"):
        result = investor_agent.run_investor_review(_df())
    assert result is None
    assert any("not on PATH" in r.message for r in caplog.records)


def test_returns_none_on_nonzero_exit(monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    fake = MagicMock()
    fake.returncode = 1
    fake.stdout = ""
    fake.stderr = "some error"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake)
    with caplog.at_level(logging.WARNING, logger="investor_agent"):
        result = investor_agent.run_investor_review(_df())
    assert result is None
    assert any("exited 1" in r.message for r in caplog.records)


def test_returns_none_on_timeout(monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired("claude", 120)
    monkeypatch.setattr("subprocess.run", raise_timeout)
    with caplog.at_level(logging.WARNING, logger="investor_agent"):
        result = investor_agent.run_investor_review(_df())
    assert result is None
    assert any("timed out" in r.message for r in caplog.records)


def test_returns_stdout_when_successful(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "NVDA stands out due to strong RS and clean VCP base."
    fake.stderr = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake)
    result = investor_agent.run_investor_review(_df())
    assert result is not None
    assert "NVDA" in result


def test_returns_none_on_empty_stdout(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = "   \n   "
    fake.stderr = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake)
    assert investor_agent.run_investor_review(_df()) is None


def test_prompt_does_not_use_bypass_permissions(monkeypatch):
    """The CLI args list must NOT include 'bypassPermissions' — read-only
    review doesn't need tool use, so a tighter permission posture is correct."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    captured = {}
    def fake_run(args, **kw):
        captured["args"] = list(args)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m
    monkeypatch.setattr("subprocess.run", fake_run)
    investor_agent.run_investor_review(_df())
    assert "bypassPermissions" not in captured["args"]


def test_caps_prompt_rows(monkeypatch):
    """A 50-row screener result should be truncated to _MAX_ROWS_IN_PROMPT."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/claude")
    captured = {}
    def fake_run(args, **kw):
        captured["prompt"] = args[-1]
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m
    monkeypatch.setattr("subprocess.run", fake_run)

    big = pd.DataFrame([{
        "rank": i + 1, "ticker": f"T{i}", "price": 100.0, "rs_score": 80.0,
        "adr": 0.04, "vcp_contractions": 2, "in_base": True,
        "vol_contracting": True, "vcp_pivot": 102.0,
        "eps_q_growth": 0.30, "rev_growth": 0.25, "eps_accel": True,
    } for i in range(50)])
    investor_agent.run_investor_review(big)

    # Only first _MAX_ROWS_IN_PROMPT tickers in the prompt
    prompt = captured["prompt"]
    assert "T0" in prompt
    assert "T19" in prompt   # _MAX_ROWS_IN_PROMPT - 1
    assert "T20" not in prompt
    assert "T49" not in prompt


import json


def _seed_strategies(tmp_path, monkeypatch):
    import quant.strategies.contract as strategies
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


# ---------------------------------------------------------------------------
# Task 7: three LLM stages — analyst, critic, PM
# ---------------------------------------------------------------------------

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
