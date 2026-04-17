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


from breakers import check_vix_spike, check_single_name_shock


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


from breakers import check_news_shock
from news_shock import NewsHit


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


from breakers import check_macro_flip


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


def test_news_shock_logs_every_hit_even_if_not_tripped(tmp_path, monkeypatch):
    """Per spec §6: every keyword hit is logged regardless of corroboration."""
    import news_shock
    log_path = tmp_path / "news_log.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    hits = [NewsHit(title="Trump floats tariff idea", source="yahoo",
                    ts=dt.datetime(2026, 4, 17, 14, tzinfo=dt.timezone.utc),
                    matched="tariff")]
    # No SPY corroboration → does NOT trip, but MUST log
    result = check_news_shock(baseline=bl, hits=hits,
                              spy_now=479.9, spy_15min_ago=479.0)
    assert result.tripped is False
    assert log_path.exists()
    content = log_path.read_text()
    assert "Trump floats tariff idea" in content
    assert "False" in content  # corroborated column


def test_news_shock_logs_hits_as_corroborated_when_tripped(tmp_path, monkeypatch):
    import news_shock
    log_path = tmp_path / "news_log.csv"
    monkeypatch.setattr(news_shock, "NEWS_SHOCK_LOG", str(log_path))

    bl = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                  news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    hits = [NewsHit(title="Fed surprise rate hike", source="yahoo",
                    ts=dt.datetime(2026, 4, 17, 14, tzinfo=dt.timezone.utc),
                    matched="fed")]
    result = check_news_shock(baseline=bl, hits=hits,
                              spy_now=476.0, spy_15min_ago=479.0)
    assert result.tripped is True
    content = log_path.read_text()
    assert "Fed surprise rate hike" in content
    assert "True" in content
