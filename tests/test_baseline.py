# tests/test_baseline.py
import datetime as dt
from unittest.mock import patch


def test_capture_baseline_calls_all_sources():
    from baseline import capture_baseline
    with patch("baseline._fetch_spy", return_value=480.0), \
         patch("baseline._fetch_vix", return_value=14.1), \
         patch("baseline._fetch_macro_score", return_value=0.12):
        bl = capture_baseline()
    assert bl.spy == 480.0
    assert bl.vix == 14.1
    assert bl.macro_score == 0.12
    assert bl.news_cursor_at.tzinfo is not None


def test_capture_baseline_returns_utc_cursor():
    from baseline import capture_baseline
    with patch("baseline._fetch_spy", return_value=480.0), \
         patch("baseline._fetch_vix", return_value=14.0), \
         patch("baseline._fetch_macro_score", return_value=0.0):
        bl = capture_baseline()
    assert bl.news_cursor_at.tzinfo == dt.timezone.utc


def test_fetch_vix_prefers_intraday_bars(monkeypatch):
    """_fetch_vix should use 5-min bars first; fall back to daily if empty."""
    import baseline
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
    import baseline
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
