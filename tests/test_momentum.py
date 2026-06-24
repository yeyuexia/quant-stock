"""Tests for momentum ranking and the new hysteresis logic."""
import sys
import os
import pandas as pd
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import momentum
import config


def _ranking_df(rows):
    """Build a DataFrame matching rank_etfs()'s output schema.

    rows: list of (ticker, momentum_score, above_sma200). Rank is assigned
    in input order so callers control the ranking explicitly.
    """
    df = pd.DataFrame([
        {"ticker": t, "momentum_score": s, "above_sma200": sma,
         "price": 100.0, "rank": i + 1}
        for i, (t, s, sma) in enumerate(rows)
    ])
    return df


# ── select_with_hysteresis: pure unit ──────────────────────────────

def test_select_no_hysteresis_returns_top_n():
    """Without held_etfs (or depth=0), behavior matches the old code path."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("EEE", 0.5, True),
    ])
    out = momentum.select_with_hysteresis(ranking, top_n=4)
    assert list(out["ticker"]) == ["AAA", "BBB", "CCC", "DDD"]


def test_select_held_slipped_one_rank_is_retained():
    """Held ETF at rank top_n+1 stays in (sticky)."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("HELD", 0.5, True),   # rank 5 — outside top-4 but held
        ("ZZZ", 0.4, True),
    ])
    out = momentum.select_with_hysteresis(
        ranking, top_n=4, held_etfs={"HELD"}, hysteresis_depth=1,
    )
    # All top-4 kept AND HELD added → 5 total
    assert set(out["ticker"]) == {"AAA", "BBB", "CCC", "DDD", "HELD"}


def test_select_held_below_sma_is_dropped():
    """Trend regime overrides hysteresis — sticky only applies if still above SMA."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("HELD", 0.5, False),  # rank 5 but below SMA → must be sold
    ])
    out = momentum.select_with_hysteresis(
        ranking, top_n=4, held_etfs={"HELD"}, hysteresis_depth=1,
    )
    assert "HELD" not in set(out["ticker"])


def test_select_held_beyond_depth_is_dropped():
    """Hysteresis_depth=1 means rank top_n+2 is too far — sell anyway."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("FILL", 0.5, True),
        ("HELD", 0.4, True),   # rank 6 — beyond depth=1
    ])
    out = momentum.select_with_hysteresis(
        ranking, top_n=4, held_etfs={"HELD"}, hysteresis_depth=1,
    )
    assert "HELD" not in set(out["ticker"])


def test_select_unheld_at_sticky_rank_not_added():
    """An ETF at rank top_n+1 that's NOT held doesn't get added."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("NOTHELD", 0.5, True),  # rank 5 but not in portfolio
    ])
    out = momentum.select_with_hysteresis(
        ranking, top_n=4, held_etfs={"SOMETHING_ELSE"}, hysteresis_depth=1,
    )
    assert list(out["ticker"]) == ["AAA", "BBB", "CCC", "DDD"]


def test_select_depth_2_accepts_held_at_n_plus_2():
    """Deeper hysteresis tolerates a bigger rank slip."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("MID", 0.5, True),     # rank 5
        ("HELD", 0.4, True),    # rank 6
    ])
    out = momentum.select_with_hysteresis(
        ranking, top_n=4, held_etfs={"HELD"}, hysteresis_depth=2,
    )
    assert "HELD" in set(out["ticker"])


# ── generate_signals integration with hysteresis ───────────────────

def test_generate_signals_weight_caps_when_sticky_inflates_holdings(monkeypatch):
    """When hysteresis adds a sticky ETF, per-pick weight drops so total stays ≤ 1."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("HELD", 0.5, True),
    ])
    monkeypatch.setattr(momentum, "rank_etfs", lambda: ranking)
    monkeypatch.setattr(momentum, "MOMENTUM_TOP_N", 4)
    monkeypatch.setattr(momentum, "MOMENTUM_HYSTERESIS_DEPTH", 1)

    sig = momentum.generate_signals(held_etfs={"HELD"})
    weights = dict([(t, w) for t, w in sig["holdings"] if t != config.SAFE_HAVEN])
    # 5 holdings, equal weight = 1/5 = 0.20 each
    assert len(weights) == 5
    for w in weights.values():
        assert abs(w - 0.20) < 1e-6
    # Sum (incl. any BIL) does not exceed 1
    assert sum(w for _, w in sig["holdings"]) <= 1.0 + 1e-6


def test_generate_signals_default_behavior_unchanged_without_held(monkeypatch):
    """Passing held_etfs=None reverts to the old top-N selection."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("HELD", 0.5, True),
    ])
    monkeypatch.setattr(momentum, "rank_etfs", lambda: ranking)
    monkeypatch.setattr(momentum, "MOMENTUM_TOP_N", 4)
    monkeypatch.setattr(momentum, "MOMENTUM_HYSTERESIS_DEPTH", 1)

    sig = momentum.generate_signals()  # no held_etfs
    tickers = [t for t, _ in sig["holdings"] if t != config.SAFE_HAVEN]
    assert set(tickers) == {"AAA", "BBB", "CCC", "DDD"}


def test_generate_signals_uses_real_rank_from_dataframe(monkeypatch):
    """holdings_ranked now reflects the ETF's universe rank, not slot index."""
    ranking = _ranking_df([
        ("AAA", 0.9, True), ("BBB", 0.8, True),
        ("CCC", 0.7, True), ("DDD", 0.6, True),
        ("HELD", 0.5, True),
    ])
    monkeypatch.setattr(momentum, "rank_etfs", lambda: ranking)
    monkeypatch.setattr(momentum, "MOMENTUM_TOP_N", 4)
    monkeypatch.setattr(momentum, "MOMENTUM_HYSTERESIS_DEPTH", 1)

    sig = momentum.generate_signals(held_etfs={"HELD"})
    ranks = {t: r for t, _w, r in sig["holdings_ranked"] if t != config.SAFE_HAVEN}
    assert ranks["HELD"] == 5   # actual rank, not slot index


def test_generate_signals_all_below_sma_returns_risk_off(monkeypatch):
    """Existing risk-off behavior preserved: nothing above SMA → 100% BIL."""
    ranking = _ranking_df([
        ("AAA", 0.9, False), ("BBB", 0.8, False),
    ])
    monkeypatch.setattr(momentum, "rank_etfs", lambda: ranking)

    sig = momentum.generate_signals(held_etfs={"AAA"})  # held but bad regime
    assert sig["regime"] == "risk-off"
    assert sig["holdings"] == [(config.SAFE_HAVEN, 1.0)]


# ======================================================================
# Post-review additions (formerly test_momentum_optimizations.py)
# ======================================================================

"""Regression tests for momentum.py cleanup (MO1, MO3, empty-frame guard)."""
import os
import sys
import pytest
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import momentum


def test_momentum_score_returns_none_on_short_history():
    """Before: returned -999 sentinel that could leak into display layer."""
    short = pd.Series([100, 101, 102])  # 3 bars, not enough for any month
    assert momentum._momentum_score(short, [1, 3, 6, 12]) is None


def test_momentum_score_real_data():
    """With 300 daily bars and monotonic +0.1%/day, momentum should be positive."""
    import numpy as np
    s = pd.Series(100 * np.cumprod(1 + np.full(300, 0.001)))
    score = momentum._momentum_score(s, [1, 3, 6, 12])
    assert score is not None
    assert score > 0


def test_rank_etfs_handles_empty_universe(monkeypatch):
    """No fetchable prices → empty DataFrame with expected columns, not crash."""
    monkeypatch.setattr(momentum, "fetch_prices",
                        lambda *a, **kw: pd.DataFrame())
    df = momentum.rank_etfs()
    assert df.empty
    # The frame still has the expected columns so callers can `.sort_values(...)`
    # or filter without missing-column KeyError.
    for col in ("ticker", "momentum_score", "above_sma200", "rank"):
        assert col in df.columns


def test_rank_etfs_skips_tickers_without_score(monkeypatch):
    """A ticker present in prices but with too-short history is skipped, not
    included with a None score."""
    import numpy as np
    # SPY has full history; SHORT has only 10 bars (below SMA_FILTER_PERIOD)
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    df = pd.DataFrame({
        "SPY": 100 * np.cumprod(1 + np.full(300, 0.0005)),
    }, index=idx)
    # SHORT ticker — only 10 valid values, rest NaN
    df["SHORT"] = [None] * 290 + list(100 + i for i in range(10))
    monkeypatch.setattr(momentum, "fetch_prices", lambda *a, **kw: df)
    monkeypatch.setattr(momentum, "ETF_UNIVERSE", ["SPY", "SHORT"])
    monkeypatch.setattr(momentum, "SMA_FILTER_PERIOD", 200)

    out = momentum.rank_etfs()
    tickers = list(out["ticker"])
    assert "SPY" in tickers
    assert "SHORT" not in tickers


def test_rank_etfs_no_safe_haven_in_universe(monkeypatch):
    """rank_etfs should fetch only ETF_UNIVERSE, not append SAFE_HAVEN."""
    captured = {}
    def fake_fetch(tickers, **kw):
        captured["tickers"] = list(tickers)
        return pd.DataFrame()
    monkeypatch.setattr(momentum, "fetch_prices", fake_fetch)
    monkeypatch.setattr(momentum, "ETF_UNIVERSE", ["SPY", "QQQ"])
    monkeypatch.setattr(momentum, "SAFE_HAVEN", "BIL")
    momentum.rank_etfs()
    assert captured["tickers"] == ["SPY", "QQQ"]
    assert "BIL" not in captured["tickers"]
