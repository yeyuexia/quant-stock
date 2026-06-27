"""Regression tests for risk.py + backtest.py small fixes."""
import os
import sys
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.risk.risk as risk
import quant.app.backtest as backtest


# ── risk.half_kelly ───────────────────────────────────────────────

def test_half_kelly_zero_loss_returns_zero():
    """Was a divide-by-zero on exact equality — now defensive."""
    assert risk.half_kelly(win_rate=0.6, avg_win=0.02, avg_loss=0) == 0.0


def test_half_kelly_tiny_loss_returns_zero():
    """Near-zero loss would produce absurd Kelly — reject."""
    assert risk.half_kelly(win_rate=0.6, avg_win=0.02, avg_loss=1e-12) == 0.0


def test_half_kelly_negative_win_returns_zero():
    assert risk.half_kelly(win_rate=0.6, avg_win=-0.01, avg_loss=0.01) == 0.0


def test_half_kelly_zero_winrate_returns_zero():
    assert risk.half_kelly(win_rate=0.0, avg_win=0.02, avg_loss=0.01) == 0.0


def test_half_kelly_typical_case():
    """60% win rate, 2:1 reward:risk → half-Kelly = (0.6 - 0.4/2) / 2 = 0.20."""
    k = risk.half_kelly(win_rate=0.6, avg_win=0.02, avg_loss=0.01)
    assert abs(k - 0.20) < 1e-6


# ── risk.position_size ────────────────────────────────────────────

def test_position_size_zero_on_bad_inputs():
    assert risk.position_size(capital=0, weight=0.1, price=100) == 0
    assert risk.position_size(capital=1000, weight=0, price=100) == 0
    assert risk.position_size(capital=1000, weight=0.1, price=0) == 0
    assert risk.position_size(capital=-100, weight=0.1, price=100) == 0


def test_position_size_caps_at_max_pct():
    # weight=0.5 but max_pct=0.25 → use 0.25
    n = risk.position_size(capital=10000, weight=0.5, price=100, max_pct=0.25)
    assert n == 25   # int(10000 * 0.25 / 100)


# ── backtest._momentum_score ───────────────────────────────────────

def test_backtest_momentum_score_returns_neg_inf_on_short():
    """Was -999; now -inf so it sorts to the bottom unambiguously."""
    s = pd.Series([100, 101, 102])
    val = backtest._momentum_score(s, end_idx=2, months=[1, 3, 6, 12])
    assert val == float("-inf")


def test_backtest_momentum_score_positive_on_real_data():
    n = 300
    s = pd.Series(100 * np.cumprod(1 + np.full(n, 0.001)))
    val = backtest._momentum_score(s, end_idx=n - 1, months=[1, 3, 6, 12])
    assert val > 0
