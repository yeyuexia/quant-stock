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
