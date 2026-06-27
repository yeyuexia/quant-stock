# tests/test_baseline.py
import datetime as dt
from unittest.mock import patch
import quant.signals.baseline as baseline
import os
import pytest
import sys


def test_capture_baseline_calls_all_sources():
    from quant.signals.baseline import capture_baseline
    with patch("quant.signals.baseline._fetch_spy", return_value=480.0), \
         patch("quant.signals.baseline._fetch_vix", return_value=14.1), \
         patch("quant.signals.baseline._fetch_macro_score", return_value=0.12):
        bl = capture_baseline()
    assert bl.spy == 480.0
    assert bl.vix == 14.1
    assert bl.macro_score == 0.12
    assert bl.news_cursor_at.tzinfo is not None


def test_capture_baseline_returns_utc_cursor():
    from quant.signals.baseline import capture_baseline
    with patch("quant.signals.baseline._fetch_spy", return_value=480.0), \
         patch("quant.signals.baseline._fetch_vix", return_value=14.0), \
         patch("quant.signals.baseline._fetch_macro_score", return_value=0.0):
        bl = capture_baseline()
    assert bl.news_cursor_at.tzinfo == dt.timezone.utc


def test_fetch_vix_prefers_intraday_bars(monkeypatch):
    """_fetch_vix should use 5-min bars first; fall back to daily if empty."""
    import quant.signals.baseline as baseline
    from unittest.mock import MagicMock, patch

    intraday_hist = MagicMock()
    intraday_hist.empty = False
    intraday_hist.__getitem__ = lambda self, k: _IntradayCloseSeries()

    daily_hist = MagicMock()
    daily_hist.empty = False
    daily_hist.__getitem__ = lambda self, k: _DailyCloseSeries()

    calls = []
    def fake_history(period=None, interval=None):
        calls.append((period, interval))
        if interval == "5m":
            return intraday_hist
        return daily_hist

    fake_ticker = MagicMock()
    fake_ticker.history = fake_history

    with patch("yfinance.Ticker", return_value=fake_ticker):
        v = baseline._fetch_vix()

    # Intraday should have been tried first
    assert calls[0] == ("1d", "5m")
    assert v == 17.5   # the intraday series's last close


def test_fetch_vix_falls_back_to_daily_when_intraday_empty(monkeypatch):
    import quant.signals.baseline as baseline
    from unittest.mock import MagicMock, patch

    intraday_hist = MagicMock()
    intraday_hist.empty = True

    daily_hist = MagicMock()
    daily_hist.empty = False
    daily_hist.__getitem__ = lambda self, k: _DailyCloseSeries()

    def fake_history(period=None, interval=None):
        if interval == "5m":
            return intraday_hist
        return daily_hist

    fake_ticker = MagicMock()
    fake_ticker.history = fake_history

    with patch("yfinance.Ticker", return_value=fake_ticker):
        v = baseline._fetch_vix()

    assert v == 14.2   # the daily series's last close


class _IntradayCloseSeries:
    """Minimal pandas-like stand-in."""
    @property
    def iloc(self):
        return _SeriesIloc([17.3, 17.4, 17.5])


class _DailyCloseSeries:
    @property
    def iloc(self):
        return _SeriesIloc([14.0, 14.1, 14.2])


class _SeriesIloc:
    def __init__(self, values):
        self._v = values
    def __getitem__(self, idx):
        return self._v[idx]


# ======================================================================
# Post-review additions (formerly test_baseline_optimizations.py)
# ======================================================================

"""Regression tests for baseline.py hardening (validation + retry + cached md client)."""
import os
import sys
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.signals.baseline as baseline


def test_capture_baseline_rejects_zero_spy(monkeypatch):
    monkeypatch.setattr(baseline, "_fetch_spy", lambda: 0.0)
    monkeypatch.setattr(baseline, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(baseline, "_fetch_macro_score", lambda: 0.0)
    with pytest.raises(RuntimeError, match="invalid spy"):
        baseline.capture_baseline()


def test_capture_baseline_rejects_negative_spy(monkeypatch):
    monkeypatch.setattr(baseline, "_fetch_spy", lambda: -1.0)
    monkeypatch.setattr(baseline, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(baseline, "_fetch_macro_score", lambda: 0.0)
    with pytest.raises(RuntimeError, match="invalid spy"):
        baseline.capture_baseline()


def test_capture_baseline_rejects_nan_vix(monkeypatch):
    monkeypatch.setattr(baseline, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(baseline, "_fetch_vix", lambda: float("nan"))
    monkeypatch.setattr(baseline, "_fetch_macro_score", lambda: 0.0)
    with pytest.raises(RuntimeError, match="invalid vix"):
        baseline.capture_baseline()


def test_capture_baseline_rejects_nan_macro(monkeypatch):
    monkeypatch.setattr(baseline, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(baseline, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(baseline, "_fetch_macro_score", lambda: float("nan"))
    with pytest.raises(RuntimeError, match="invalid macro"):
        baseline.capture_baseline()


def test_retry_succeeds_on_second_attempt():
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return 480.0
    assert baseline._retry(flaky) == 480.0
    assert calls["n"] == 2


def test_md_client_is_cached_across_calls(monkeypatch):
    """Module-level _MD_CLIENT — second access returns same instance."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_API_SECRET", "test")
    monkeypatch.setattr(baseline, "_MD_CLIENT", None)

    construct_count = {"n": 0}

    class FakeClient:
        def __init__(self, **kw):
            construct_count["n"] += 1

    with patch("alpaca.data.historical.StockHistoricalDataClient", FakeClient):
        c1 = baseline._md_client()
        c2 = baseline._md_client()
    assert c1 is c2
    assert construct_count["n"] == 1


def test_md_client_raises_when_creds_missing(monkeypatch):
    monkeypatch.setattr(baseline, "_MD_CLIENT", None)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
        baseline._md_client()
