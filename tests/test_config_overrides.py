import importlib
import json
import os


def _reload_config(tmp_path, monkeypatch, overrides=None):
    """Helper: point config's override path at tmp and reload the module."""
    import config
    override_path = tmp_path / "strategy_overrides.json"
    if overrides is not None:
        override_path.write_text(json.dumps(overrides))
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(override_path))
    config._apply_overrides()
    return config


def test_valid_stop_loss_override_applies(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    cfg = _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": 0.075})
    assert cfg.STOP_LOSS_PCT == 0.075
    config.STOP_LOSS_PCT = original


def test_unknown_key_is_ignored(tmp_path, monkeypatch):
    import config
    original_max_orders = config.DAILY_MAX_ORDERS
    _reload_config(tmp_path, monkeypatch, {"DAILY_MAX_ORDERS": 999999})
    assert config.DAILY_MAX_ORDERS == original_max_orders


def test_type_mismatch_is_ignored(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": "not a float"})
    assert config.STOP_LOSS_PCT == original


def test_out_of_bounds_is_ignored(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    _reload_config(tmp_path, monkeypatch, {"STOP_LOSS_PCT": 0.99})
    assert config.STOP_LOSS_PCT == original


def test_missing_file_leaves_defaults_intact(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(tmp_path / "nope.json"))
    config._apply_overrides()
    assert config.STOP_LOSS_PCT == original


def test_corrupt_json_leaves_defaults_intact(tmp_path, monkeypatch):
    import config
    original = config.STOP_LOSS_PCT
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(config, "_OVERRIDES_PATH", str(p))
    config._apply_overrides()
    assert config.STOP_LOSS_PCT == original


def test_watchlist_and_keywords_lists_apply(tmp_path, monkeypatch):
    import config
    _reload_config(tmp_path, monkeypatch, {
        "WATCHLIST": config.WATCHLIST + ["PLTR", "SMCI"],
        "NEWS_SHOCK_KEYWORDS": config.NEWS_SHOCK_KEYWORDS + ["nvda"],
    })
    assert "PLTR" in config.WATCHLIST
    assert "SMCI" in config.WATCHLIST
    assert "nvda" in config.NEWS_SHOCK_KEYWORDS
