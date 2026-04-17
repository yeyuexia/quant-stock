# tests/test_breakers.py
import datetime as dt
from pending_plan import Baseline
from breakers import check_spy_drop, BreakerResult


def _baseline(spy=480.0):
    return Baseline(
        spy=spy, vix=14.0, macro_score=0.0,
        news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc),
    )


def test_spy_drop_trips_at_threshold():
    bl = _baseline(spy=480.0)
    # −1.5% exactly → 472.80
    result = check_spy_drop(bl, spy_now=472.79)
    assert result.tripped is True
    assert result.breaker == "A"
    assert "1.5%" in result.message or "spy" in result.message.lower()


def test_spy_drop_does_not_trip_just_below_threshold():
    bl = _baseline(spy=480.0)
    result = check_spy_drop(bl, spy_now=472.90)  # 1.48% drop
    assert result.tripped is False


def test_spy_drop_does_not_trip_on_up_move():
    bl = _baseline(spy=480.0)
    result = check_spy_drop(bl, spy_now=485.0)
    assert result.tripped is False


def test_breaker_result_has_scope():
    bl = _baseline()
    result = check_spy_drop(bl, spy_now=470.0)
    assert result.scope == "buys"
    assert result.affected_symbols is None
