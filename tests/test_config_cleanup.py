"""Regression tests for the config.py cleanup pass (C1, C3-C5, C7, C8, C12,
C13, C15, C16, C18, C27)."""
import json
import logging
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.config as config


# ── C1, C3-C5, C7, C8, C12, C13: dead constants are gone ──────────

def test_dead_constants_removed():
    """Confirm the cleanup actually deleted the constants — guards against
    someone re-adding them in a future PR."""
    dead = [
        "REBALANCE_FREQUENCY_DAYS",
        "SCREEN_MIN_MARKET_CAP",
        "SCREEN_MAX_MARKET_CAP",
        "SCREEN_TIGHTNESS_PCT_MAX",
        "MAX_POSITIONS",
        "WATCHDOG_BUY_OPEN_DECAY",
        "ALPACA_LIVE_CONFIRM",      # broker.py reads os.environ directly now
        "ALPACA_PAPER_URL",
        "ALPACA_LIVE_URL",
    ]
    for name in dead:
        assert not hasattr(config, name), \
            f"config.{name} should be removed (dead constant)"


def test_aggressive_params_no_max_position_pct():
    """C8: aggressive top_n=2 equal-weight makes max_position_pct redundant."""
    assert "max_position_pct" not in config.AGGRESSIVE_PARAMS


def test_screen_tightness_not_in_override_schema():
    """C4: SCREEN_TIGHTNESS_PCT_MAX gave quant subagent a false impression
    it could tune a real knob. Schema must no longer list it."""
    assert "SCREEN_TIGHTNESS_PCT_MAX" not in config._OVERRIDE_SCHEMA


# ── C18: unknown PORTFOLIO_MODE raises ─────────────────────────────

def test_unknown_portfolio_mode_raises(monkeypatch):
    """Typing PORTFOLIO_MODE=baalanced must raise, not silently fall back."""
    monkeypatch.setenv("PORTFOLIO_MODE", "baalanced")
    # Force re-import of config to re-evaluate module-level code
    import importlib
    with pytest.raises(ValueError, match="unknown PORTFOLIO_MODE"):
        importlib.reload(config)
    # Restore a valid mode so other tests don't fail
    monkeypatch.setenv("PORTFOLIO_MODE", "balanced")
    importlib.reload(config)


def test_valid_portfolio_modes_accepted(monkeypatch):
    import importlib
    for mode in ("conservative", "balanced", "growth"):
        monkeypatch.setenv("PORTFOLIO_MODE", mode)
        importlib.reload(config)
        assert config.PORTFOLIO_MODE == mode
    monkeypatch.setenv("PORTFOLIO_MODE", "balanced")
    importlib.reload(config)


# ── C15: list override length validation ──────────────────────────

def test_apply_overrides_rejects_empty_list(tmp_path, monkeypatch, caplog):
    """A WATCHLIST override with 0 entries (below lo=1) must be rejected."""
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text(json.dumps({"WATCHLIST": []}))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(overrides_file))

    before = list(config.WATCHLIST)
    with caplog.at_level(logging.WARNING):
        config._apply_overrides()
    # Unchanged; warning emitted
    assert config.WATCHLIST == before
    assert any("out of bounds" in r.message for r in caplog.records)


def test_apply_overrides_rejects_oversized_list(tmp_path, monkeypatch, caplog):
    """A WATCHLIST override with 1000 entries (above hi=200) must be rejected."""
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text(json.dumps({"WATCHLIST": [f"T{i}" for i in range(1000)]}))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(overrides_file))

    before = list(config.WATCHLIST)
    with caplog.at_level(logging.WARNING):
        config._apply_overrides()
    assert config.WATCHLIST == before


def test_apply_overrides_accepts_valid_list(tmp_path, monkeypatch):
    overrides_file = tmp_path / "overrides.json"
    new_list = ["AAPL", "MSFT", "NVDA"]
    overrides_file.write_text(json.dumps({"WATCHLIST": new_list}))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(overrides_file))

    saved = list(config.WATCHLIST)
    try:
        config._apply_overrides()
        assert config.WATCHLIST == new_list
    finally:
        config.WATCHLIST = saved


def test_apply_overrides_rejects_wrong_type(tmp_path, monkeypatch, caplog):
    """STOP_LOSS_PCT (float) override that's a string must be rejected."""
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text(json.dumps({"STOP_LOSS_PCT": "not a number"}))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(overrides_file))

    before = config.STOP_LOSS_PCT
    with caplog.at_level(logging.WARNING):
        config._apply_overrides()
    assert config.STOP_LOSS_PCT == before


def test_apply_overrides_rejects_forbidden_key(tmp_path, monkeypatch, caplog):
    """Keys not in _OVERRIDE_SCHEMA (e.g. credentials, safety rails) ignored."""
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text(json.dumps({
        "ALPACA_API_KEY": "evil_key_injection",
        "HALT_PATH": "/dev/null",
    }))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(overrides_file))

    api_key_before = config.ALPACA_API_KEY
    halt_before = config.HALT_PATH
    with caplog.at_level(logging.WARNING):
        config._apply_overrides()
    assert config.ALPACA_API_KEY == api_key_before
    assert config.HALT_PATH == halt_before
    assert any("unknown/forbidden" in r.message for r in caplog.records)


# ── C16: conftest isolates _apply_overrides from local .cache ─────

def test_overrides_path_is_isolated_to_tmp_path(tmp_path, request):
    """The autouse fixture should have redirected _OVERRIDES_PATH into
    pytest's tmp_path for this test — verify by checking the path."""
    # Conftest redirects to a per-test tmp dir under _isolated/config/.
    assert "_isolated" in config._OVERRIDES_PATH
    assert "strategy_overrides.json" in config._OVERRIDES_PATH
    assert not os.path.exists(config._OVERRIDES_PATH)


# ── C27: ETF_LEVERAGED rename + back-compat alias ─────────────────

def test_etf_leveraged_public_name_exists():
    assert hasattr(config, "ETF_LEVERAGED")
    assert "TQQQ" in config.ETF_LEVERAGED


def test_etf_leveraged_underscore_alias_still_works():
    """Back-compat: old callers reading config._ETF_LEVERAGED keep working."""
    assert config._ETF_LEVERAGED is config.ETF_LEVERAGED


# ── C19: docstring no longer mentions "weekly" or hard dollar amounts ──

def test_docstring_does_not_promise_weekly_aggressive():
    """The aggressive tranche is now daily — docstring must not lie about
    'weekly' (it did before the daily-cadence change)."""
    doc = (config.__doc__ or "").lower()
    assert "weekly" not in doc


def test_docstring_does_not_hardcode_dollar_amounts():
    """Tranche sizes are dynamic (snap.equity × pct) — docstring should not
    list $90,000 / $10,000 as if they were constants."""
    doc = config.__doc__ or ""
    assert "$90,000" not in doc
    assert "$10,000" not in doc
