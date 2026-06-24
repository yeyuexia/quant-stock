"""Regression tests for run.py rewrite (R1, R3, R5, R6, R8, R13, R15, R17,
R19, R20, R21)."""
import io
import os
import sys
import pytest
import numpy as np
import pandas as pd
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run
import config


# ── R8: argparse — --section / --skip / --backtest-years ─────────────

def test_main_runs_only_named_section(capsys, monkeypatch):
    """--section macro should call only run_macro_regime."""
    called = []
    monkeypatch.setattr(run, "run_macro_regime", lambda: called.append("macro"))
    monkeypatch.setattr(run, "run_momentum_strategy", lambda: called.append("momentum"))
    monkeypatch.setattr(run, "run_stock_screener", lambda **kw: called.append("screener"))
    monkeypatch.setattr(run, "run_alpaca_holdings", lambda: called.append("alpaca"))
    monkeypatch.setattr(run, "run_risk_analysis", lambda s: called.append("risk"))
    monkeypatch.setattr(run, "run_sentiment", lambda: called.append("sentiment"))
    monkeypatch.setattr(run, "run_backtest", lambda years=5: called.append(f"backtest({years})"))

    run.main(["--section", "macro"])
    assert called == ["macro"]


def test_main_skip_section(capsys, monkeypatch):
    """--skip backtest --skip alpaca should run everything else."""
    called = []
    for name in ("run_macro_regime", "run_momentum_strategy",
                 "run_alpaca_holdings", "run_risk_analysis",
                 "run_sentiment", "run_backtest"):
        monkeypatch.setattr(run, name,
                            (lambda n=name: lambda *a, **kw: called.append(n))())
    monkeypatch.setattr(run, "run_stock_screener", lambda **kw: called.append("screener"))

    run.main(["--skip", "backtest", "--skip", "alpaca"])
    assert "run_backtest" not in called
    assert "run_alpaca_holdings" not in called
    assert "run_macro_regime" in called
    assert "screener" in called


def test_main_backtest_years_flag(monkeypatch):
    captured = {}
    monkeypatch.setattr(run, "run_macro_regime", lambda: None)
    monkeypatch.setattr(run, "run_momentum_strategy", lambda: None)
    monkeypatch.setattr(run, "run_stock_screener", lambda **kw: None)
    monkeypatch.setattr(run, "run_alpaca_holdings", lambda: None)
    monkeypatch.setattr(run, "run_risk_analysis", lambda s: None)
    monkeypatch.setattr(run, "run_sentiment", lambda: None)
    monkeypatch.setattr(run, "run_backtest",
                        lambda years=5: captured.setdefault("years", years))

    run.main(["--section", "backtest", "--backtest-years", "10"])
    assert captured["years"] == 10


# ── R19: investor_agent default off, --with-review opt-in ──────────

def test_screener_default_off_investor_review(monkeypatch):
    """run_stock_screener default with_review=False — no LLM cost on daily."""
    called = {}
    def fake_screen(with_review=False):
        called["with_review"] = with_review
        return pd.DataFrame()
    monkeypatch.setattr("screener.screen_stocks", fake_screen)

    run.run_stock_screener()
    assert called["with_review"] is False


def test_screener_with_review_flag(monkeypatch):
    called = {}
    def fake_screen(with_review=False):
        called["with_review"] = with_review
        return (pd.DataFrame(), None)
    monkeypatch.setattr("screener.screen_stocks", fake_screen)

    run.run_stock_screener(with_review=True)
    assert called["with_review"] is True


def test_main_passes_with_review_to_screener(monkeypatch):
    captured = {}
    monkeypatch.setattr(run, "run_macro_regime", lambda: None)
    monkeypatch.setattr(run, "run_momentum_strategy", lambda: None)
    monkeypatch.setattr(run, "run_stock_screener",
                        lambda **kw: captured.update(kw))
    monkeypatch.setattr(run, "run_alpaca_holdings", lambda: None)
    monkeypatch.setattr(run, "run_risk_analysis", lambda s: None)
    monkeypatch.setattr(run, "run_sentiment", lambda: None)
    monkeypatch.setattr(run, "run_backtest", lambda years=5: None)

    run.main(["--section", "screener", "--with-review"])
    assert captured.get("with_review") is True


# ── R6 + R15: risk analysis uses real weights + SAFE_HAVEN ─────────

def test_run_risk_analysis_uses_signal_weights(monkeypatch, capsys):
    """signals's holdings carry per-ticker weights; risk_analysis must
    use them after renormalizing over non-SAFE_HAVEN holdings."""
    monkeypatch.setattr(config, "SAFE_HAVEN", "BIL")

    captured = {}
    def fake_portfolio_stats(returns, weights):
        captured["weights"] = list(weights)
        return {
            "ann_return": 0.10, "ann_volatility": 0.15, "sharpe_ratio": 0.67,
            "max_drawdown": -0.10, "var_95_daily": -0.02, "cvar_95_daily": -0.03,
            "win_rate": 0.55, "best_day": 0.04, "worst_day": -0.05,
        }
    monkeypatch.setattr("risk.portfolio_stats", fake_portfolio_stats)
    monkeypatch.setattr("risk.correlation_matrix",
                        lambda r: pd.DataFrame())
    monkeypatch.setattr("risk.diversification_ratio",
                        lambda r, w: 1.5)
    monkeypatch.setattr("data.fetch_prices",
                        lambda t, period="1y": pd.DataFrame(
                            {x: [100, 101, 102] for x in t},
                            index=pd.date_range("2026-01-01", periods=3),
                        ))

    signals = {"holdings": [("SPY", 0.5), ("QQQ", 0.3), ("BIL", 0.2)]}
    run.run_risk_analysis(signals)

    # BIL excluded; remaining SPY=0.5, QQQ=0.3 → renormalized 0.625/0.375
    assert len(captured["weights"]) == 2
    assert abs(captured["weights"][0] - 0.625) < 1e-6
    assert abs(captured["weights"][1] - 0.375) < 1e-6


def test_run_risk_analysis_uses_config_safe_haven(monkeypatch):
    """Filter must use config.SAFE_HAVEN, not hardcoded 'BIL'."""
    monkeypatch.setattr(config, "SAFE_HAVEN", "SHY")

    captured = {}
    def fake_portfolio_stats(returns, weights):
        captured["weights"] = list(weights)
        captured["tickers_count"] = len(weights)
        return {
            "ann_return": 0.0, "ann_volatility": 0.0, "sharpe_ratio": 0.0,
            "max_drawdown": 0.0, "var_95_daily": 0.0, "cvar_95_daily": 0.0,
            "win_rate": 0.0, "best_day": 0.0, "worst_day": 0.0,
        }
    monkeypatch.setattr("risk.portfolio_stats", fake_portfolio_stats)
    monkeypatch.setattr("risk.correlation_matrix",
                        lambda r: pd.DataFrame())
    monkeypatch.setattr("risk.diversification_ratio",
                        lambda r, w: 1.0)
    monkeypatch.setattr("data.fetch_prices",
                        lambda t, period="1y": pd.DataFrame(
                            {x: [100, 101] for x in t},
                            index=pd.date_range("2026-01-01", periods=2),
                        ))

    # SHY is the safe haven; BIL is just a normal holding
    signals = {"holdings": [("SPY", 0.5), ("BIL", 0.3), ("SHY", 0.2)]}
    run.run_risk_analysis(signals)
    # SHY excluded → 2 tickers remain (SPY, BIL)
    assert captured["tickers_count"] == 2


def test_run_risk_analysis_all_safe_haven_returns(monkeypatch, capsys):
    monkeypatch.setattr(config, "SAFE_HAVEN", "BIL")
    signals = {"holdings": [("BIL", 1.0)]}
    run.run_risk_analysis(signals)
    out = capsys.readouterr().out
    assert "safe haven" in out.lower()


# ── R21: safe_section re-raises bug-class exceptions ───────────────

def test_safe_section_propagates_attribute_error():
    """Bug-class exceptions (AttributeError / NameError / TypeError) must
    re-raise so they're not silently swallowed under 'section failed'."""
    def broken():
        raise AttributeError("undefined attr")
    with pytest.raises(AttributeError):
        run.safe_section("broken", broken)


def test_safe_section_swallows_runtime_errors():
    """Genuine runtime errors (network, data) should not crash main."""
    def transient():
        raise RuntimeError("network blip")
    result = run.safe_section("transient", transient)
    assert result is None


# ── R5: screener description reads RS from config ──────────────────

def test_screener_section_reads_rs_from_config(monkeypatch, capsys):
    monkeypatch.setattr(config, "SCREEN_RS_MIN", 88.0)  # arbitrary distinct value
    monkeypatch.setattr("screener.screen_stocks",
                        lambda **kw: pd.DataFrame())
    run.run_stock_screener()
    out = capsys.readouterr().out
    assert "RS ≥88" in out


# ── R13: date header is ET (or local fallback) ─────────────────────

def test_now_et_str_includes_timezone_marker():
    result = run._now_et_str()
    # Either ET marker (zoneinfo OK) or "(local)" fallback
    assert "ET" in result or "(local)" in result


# ── R18: momentum section mentions hysteresis when nonzero ─────────

def test_momentum_section_mentions_hysteresis(monkeypatch, capsys):
    monkeypatch.setattr(config, "MOMENTUM_HYSTERESIS_DEPTH", 2)
    monkeypatch.setattr("momentum.generate_signals", lambda: {
        "ranking": pd.DataFrame([{
            "rank": 1, "ticker": "SPY", "price": 100.0,
            "1m_ret": 0.01, "3m_ret": 0.05, "6m_ret": 0.10, "12m_ret": 0.20,
            "momentum_score": 0.5, "above_sma200": True,
        }]),
        "holdings": [("SPY", 1.0)],
        "regime": "risk-on",
    })
    run.run_momentum_strategy()
    out = capsys.readouterr().out
    assert "hysteresis" in out


# ── R20: lazy imports — broker import lives in run_alpaca_holdings ──

def test_alpaca_section_lazy_imports_broker():
    """broker import must be inside run_alpaca_holdings, not at module top —
    so a broken broker module doesn't crash the whole report."""
    import ast
    with open(run.__file__) as f:
        tree = ast.parse(f.read())
    top_level_imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and getattr(node, "col_offset", 0) == 0:
            if isinstance(node, ast.ImportFrom):
                top_level_imports.append(node.module or "")
            else:
                for alias in node.names:
                    top_level_imports.append(alias.name)
    # broker must NOT be at module top level
    assert "broker" not in top_level_imports
