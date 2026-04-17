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
