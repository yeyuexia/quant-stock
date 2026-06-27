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
