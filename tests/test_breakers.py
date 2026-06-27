# tests/test_breakers.py
import datetime as dt
from quant.execution.pending_plan import Baseline
from quant.execution.breakers import check_spy_drop, BreakerResult


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


from quant.execution.breakers import check_vix_spike, check_single_name_shock


def test_vix_spike_trips_on_multiplier():
    bl = _baseline()
    bl = Baseline(spy=bl.spy, vix=18.0, macro_score=bl.macro_score,
                  news_cursor_at=bl.news_cursor_at)
    # multiplier 1.5 → 27.0, absolute floor 25.0 → threshold max is 27.0
    result = check_vix_spike(bl, vix_now=27.1)
    assert result.tripped is True
    assert result.scope == "buys"


def test_vix_spike_does_not_trip_below_absolute_floor():
    bl = Baseline(spy=480.0, vix=10.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    # multiplier 1.5 → 15.0, but absolute floor 25.0 not reached
    result = check_vix_spike(bl, vix_now=20.0)
    assert result.tripped is False


def test_vix_spike_trips_on_absolute_with_large_baseline():
    bl = Baseline(spy=480.0, vix=30.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    # Multiplier: 45.0, absolute 25.0 → max = 45.0
    result = check_vix_spike(bl, vix_now=46.0)
    assert result.tripped is True


def test_single_name_shock_affects_only_one_symbol():
    bl = _baseline()
    prices = {"AAPL": 170.0, "MSFT": 390.0}
    baselines = {"AAPL": 180.0, "MSFT": 400.0}   # AAPL -5.56%, MSFT -2.5%
    results = check_single_name_shock(bl, baselines, prices)
    tripped = [r for r in results if r.tripped]
    assert len(tripped) == 1
    assert tripped[0].affected_symbols == ["AAPL"]
    assert tripped[0].scope == "symbol"
    assert tripped[0].breaker == "C"


def test_single_name_shock_no_trip_if_all_above_threshold():
    bl = _baseline()
    prices = {"AAPL": 178.0, "MSFT": 395.0}
    baselines = {"AAPL": 180.0, "MSFT": 400.0}
    results = check_single_name_shock(bl, baselines, prices)
    assert all(not r.tripped for r in results)


from quant.execution.breakers import check_news_shock
from quant.signals.news_shock import NewsHit


def test_news_shock_requires_corroboration():
    bl = _baseline(spy=480.0)
    hits = [NewsHit(title="Trump threatens new tariffs",
                    source="yahoo",
                    ts=dt.datetime(2026, 4, 17, 14, 0, tzinfo=dt.timezone.utc),
                    matched="tariffs")]
    result = check_news_shock(
        baseline=bl, hits=hits,
        spy_now=479.9,
        spy_15min_ago=479.0,
    )
    assert result.tripped is False


def test_news_shock_trips_when_corroborated():
    bl = _baseline(spy=480.0)
    hits = [NewsHit(title="Fed surprise rate hike",
                    source="yahoo",
                    ts=dt.datetime(2026, 4, 17, 14, 0, tzinfo=dt.timezone.utc),
                    matched="fed")]
    result = check_news_shock(
        baseline=bl, hits=hits,
        spy_now=476.0, spy_15min_ago=479.0,
    )
    assert result.tripped is True
    assert result.breaker == "D"
    assert result.scope == "buys"


def test_news_shock_no_hits_never_trips():
    bl = _baseline()
    result = check_news_shock(baseline=bl, hits=[], spy_now=470.0, spy_15min_ago=480.0)
    assert result.tripped is False


from quant.execution.breakers import check_macro_flip
from quant.execution.breakers import (
    check_spy_drop, check_vix_spike, check_news_shock,
)
import os
import pytest
import sys


def test_macro_flip_trips_on_score_drop():
    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    # Drop of 0.35 → exceeds 0.3 threshold
    result = check_macro_flip(bl, macro_now=-0.15)
    assert result.tripped is True
    assert result.breaker == "E"
    assert result.scope == "risk_on_buys"


def test_macro_flip_does_not_trip_small_drop():
    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.20,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    result = check_macro_flip(bl, macro_now=0.05)  # drop of 0.15
    assert result.tripped is False


def test_macro_flip_ignores_improvement():
    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    result = check_macro_flip(bl, macro_now=0.5)
    assert result.tripped is False


def test_news_shock_does_not_log_inside_breaker(tmp_path, monkeypatch):
    """check_news_shock is now pure — audit logging was moved to the
    executor caller. This test confirms the breaker never touches disk
    (the executor-side test covers that the log still happens)."""
    import quant.signals.news_shock as news_shock
    log_path = tmp_path / "news_log.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    hits = [NewsHit(title="Trump floats tariff idea", source="yahoo",
                    ts=dt.datetime(2026, 4, 17, 14, tzinfo=dt.timezone.utc),
                    matched="tariff")]
    # Not corroborated
    not_trip = check_news_shock(baseline=bl, hits=hits,
                                spy_now=479.9, spy_15min_ago=479.0)
    assert not_trip.tripped is False
    # Corroborated
    trip = check_news_shock(baseline=bl, hits=hits,
                            spy_now=476.0, spy_15min_ago=479.0)
    assert trip.tripped is True
    # NEITHER call should have written to the audit log — logging is the
    # caller's (executor's) responsibility now.
    assert not log_path.exists()


# ======================================================================
# Post-review additions (formerly test_breakers_optimizations.py)
# ======================================================================

"""Regression tests for breakers.py — baseline zero-guard + pure function contract."""
import os
import sys
import datetime as dt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from quant.execution.breakers import (
    check_spy_drop, check_vix_spike, check_news_shock,
)
from quant.execution.pending_plan import Baseline


def _baseline_opt(spy=480.0, vix=14.0, macro=0.20):
    return Baseline(spy=spy, vix=vix, macro_score=macro,
                    news_cursor_at=dt.datetime.now(dt.timezone.utc))


# ── BR1: baseline.spy == 0 returns not-tripped, doesn't divide-by-zero ───

def test_check_spy_drop_handles_zero_baseline():
    """Defensive: capture_baseline now validates SPY > 0, but the breaker
    used to crash if a manually-built Baseline had 0."""
    bad = _baseline_opt(spy=0.0)
    result = check_spy_drop(bad, spy_now=470.0)
    assert result.tripped is False
    assert "invalid baseline" in result.message.lower()


def test_check_spy_drop_handles_negative_baseline():
    bad = _baseline_opt(spy=-1.0)
    result = check_spy_drop(bad, spy_now=470.0)
    assert result.tripped is False


# ── BR2: same for VIX ────────────────────────────────────────────

def test_check_vix_spike_handles_zero_baseline():
    bad = _baseline_opt(vix=0.0)
    result = check_vix_spike(bad, vix_now=30.0)
    assert result.tripped is False
    assert "invalid baseline" in result.message.lower()


# ── BR4: check_news_shock is pure — no log_hit side effect ────────

def test_check_news_shock_does_not_write_disk(monkeypatch, tmp_path):
    """The audit log used to be written inside check_news_shock — moved to
    executor. Verify the breaker itself never touches the log file."""
    import quant.signals.news_shock as news_shock
    log_path = tmp_path / "news_shock_log.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    # Build a hit that would have triggered log_hit
    hit = news_shock.NewsHit(
        title="Fed surprise cut",
        source="yahoo",
        ts=dt.datetime.now(dt.timezone.utc),
        matched="fed",
    )
    base = _baseline_opt()

    # Call the breaker — should NOT touch the log file
    result = check_news_shock(
        baseline=base, hits=[hit],
        spy_now=480.0, spy_15min_ago=480.0,   # 0% move → not tripped
    )
    assert result.tripped is False
    # Log file must not have been written
    assert not log_path.exists()


def test_check_news_shock_still_returns_correct_signal_when_tripped():
    """Functional check: corroborated news shock trips even without log_hit."""
    import quant.signals.news_shock as news_shock
    hit = news_shock.NewsHit(
        title="Fed emergency rate cut",
        source="yahoo",
        ts=dt.datetime.now(dt.timezone.utc),
        matched="fed",
    )
    base = _baseline_opt()
    # SPY moved 1% in 15 min — corroborated (default threshold 0.5%)
    result = check_news_shock(
        baseline=base, hits=[hit],
        spy_now=485.0, spy_15min_ago=480.0,
    )
    assert result.tripped is True
    assert result.scope == "buys"


def test_check_news_shock_empty_hits_returns_early():
    base = _baseline_opt()
    result = check_news_shock(
        baseline=base, hits=[],
        spy_now=485.0, spy_15min_ago=480.0,
    )
    assert result.tripped is False
    assert "no news hits" in result.message.lower()
