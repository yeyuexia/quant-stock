import math
import pytest
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
