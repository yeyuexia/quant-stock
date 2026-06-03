"""Shared pytest fixtures — auto-applied to every test in tests/."""
import os
import pytest
from unittest.mock import patch


# Per-(module, attr) pairs that point at persistent on-disk state. A single
# autouse fixture redirects every one of them to a per-test tmp file so test
# runs never read or clobber the developer's local .cache / portfolio.json /
# daily_log.csv / pending_orders.json / etc. Tests that *want* to inspect a
# write still work — they read the redirected path via the same module attr.
#
# Format: (import_path, attr_name, tmp_filename_or_None_if_dir)
_ISOLATED_PATHS = [
    # config — shared between most modules
    ("config", "_OVERRIDES_PATH",          "strategy_overrides.json"),
    ("config", "TELEGRAM_NOTIFY_PATH",     "telegram_notifications.json"),
    ("config", "HALT_PATH",                "HALT"),
    ("config", "ENTRY_PIVOTS_PATH",        "entry_pivots.json"),
    ("config", "PENDING_ORDERS_PATH",      "pending_orders.json"),
    ("config", "PENDING_PLAN_PATH",        "pending_plan.json"),
    ("config", "WATCHLIST_AUTO_PATH",      "watchlist_auto.json"),
    # orders — re-binds several config paths at import; patch its copies too
    ("orders", "PORTFOLIO_PATH",           "portfolio.json"),
    ("orders", "DAILY_LOG_PATH",           "orders_events.csv"),
    ("orders", "HALT_PATH",                "HALT"),
    ("orders", "ENTRY_PIVOTS_PATH",        "entry_pivots.json"),
    ("orders", "PENDING_ORDERS_PATH",      "pending_orders.json"),
    # executor — also re-binds HALT_PATH at import
    ("executor", "HALT_PATH",              "HALT"),
    # pending_plan
    ("pending_plan", "PENDING_PLAN_PATH",  "pending_plan.json"),
    # watchdog — operational sentinels + persistent caches
    ("watchdog", "_DEGRADED_SENTINEL_PATH","tg.json"),
    ("watchdog", "_MACRO_SCORE_PATH",      "macro_score.json"),
    ("watchdog", "_SCREENER_CACHE_PATH",   "screener_result.json"),
    ("watchdog", "_BUY_SIGNALS_TODAY_PATH","buy_signals_today.json"),
    # quant.applier — every artifact it writes
    ("quant.applier", "OVERRIDES_PATH",    "strategy_overrides.json"),
    ("quant.applier", "PROPOSALS_PATH",    "strategy_proposals.json"),
    ("quant.applier", "TG_NOTIFY_PATH",    "telegram_notifications.json"),
    ("quant.applier", "AUDIT_LOG_PATH",    "quant_review.log"),
    ("quant.applier", "DRY_RUN_PATH",      "quant_review_dry.json"),
]


@pytest.fixture(autouse=True)
def _isolate_persistent_state(tmp_path, monkeypatch):
    """Redirect every persistent state path to a per-test tmp file.

    Without this, tests pollute the developer's local .cache/ and root-dir
    state files (portfolio.json, daily_log.csv, pending_orders.json, etc.),
    and runs become order-dependent. Tests that want to override a specific
    path can still monkeypatch it after this fixture — the later patch wins.
    """
    import importlib
    isolated_root = tmp_path / "_isolated"
    isolated_root.mkdir(exist_ok=True)
    for mod_path, attr, fname in _ISOLATED_PATHS:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        if not hasattr(mod, attr):
            continue
        # Per-module subdir so same-named files (e.g. HALT, telegram_notifications.json)
        # from different modules don't collide.
        sub = isolated_root / mod_path.replace(".", "_")
        sub.mkdir(exist_ok=True)
        monkeypatch.setattr(mod, attr, str(sub / fname))


@pytest.fixture(autouse=True)
def _mock_fetch_fundamentals():
    """Prevent fetch_fundamentals from hitting the network during tests.
    Returns {} so _fundamental_ok fails open (all tickers pass the filter)."""
    try:
        with patch("screener.fetch_fundamentals", return_value={}):
            yield
    except (ImportError, AttributeError):
        yield


@pytest.fixture(autouse=True)
def _mock_record_screener_pass():
    """Stop screener.screen_stocks from writing .cache/discovery_lastpass.json
    during tests. Tests that care about the hook (test_screener) override this
    fixture locally to inspect calls."""
    try:
        with patch("screener.record_screener_pass") as m:
            yield m
    except (ImportError, AttributeError):
        yield None
