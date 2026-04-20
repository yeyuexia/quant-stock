# tests/test_config_intraday.py
import config


def test_circuit_breaker_defaults_present():
    cb = config.CIRCUIT_BREAKERS
    assert cb["spy_drop_pct"] == 0.015
    assert cb["vix_multiplier"] == 1.5
    assert cb["vix_absolute"] == 25.0
    assert cb["single_name_drop_pct"] == 0.05
    assert cb["news_corroboration_pct"] == 0.005
    assert cb["news_window_minutes"] == 15
    assert cb["news_dedupe_minutes"] == 60
    assert cb["macro_drop"] == 0.3


def test_execution_tiers_present():
    assert config.EXECUTION_TIERS["HIGH"] == {"etf_bps": 50, "stock_bps": 100}
    assert config.EXECUTION_TIERS["MED"] == {"etf_bps": 30, "stock_bps": 50}
    assert config.AGGRESSIVE_TIER_MULTIPLIER == 1.5
    assert config.MACRO_EXIT_TOLERANCE_BPS == 150


def test_slice_counts():
    assert config.SLICE_COUNTS["HIGH"] == {"small": 2, "large": 2}
    assert config.SLICE_COUNTS["MED"] == {"small": 2, "large": 4}
    assert config.SLICE_SIZE_SMALL_MAX == 2000.0


def test_defensive_symbols():
    assert {"BIL", "SHY", "IEF", "TLT"} <= config.DEFENSIVE_SYMBOLS


def test_executor_window():
    assert config.EXECUTOR_WINDOW_START == "10:00"
    assert config.EXECUTOR_WINDOW_END == "15:50"
    assert config.EXECUTOR_TICK_MINUTES == 10
    assert config.PLANNER_DIRECT_SUBMIT_THRESHOLD == 500.0
    assert config.EXECUTOR_SHADOW_MODE is False  # live submission enabled


def test_news_shock_keywords():
    kws = config.NEWS_SHOCK_KEYWORDS
    for needed in ("tariff", "fed", "powell", "recession", "war"):
        assert needed in kws


def test_daily_max_orders_bumped():
    # Slice-per-tick model requires higher ceiling
    assert config.DAILY_MAX_ORDERS >= 40
