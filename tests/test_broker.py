"""Broker construction + live-confirm guard."""
import os
import pytest
from broker import Broker, ConfigError


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("ALPACA_ENV", "ALPACA_LIVE_CONFIRM", "ALPACA_API_KEY", "ALPACA_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_paper_env_constructs_with_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    b = Broker(env="paper")
    assert b.env == "paper"


def test_live_requires_confirm_flag(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    with pytest.raises(ConfigError, match="ALPACA_LIVE_CONFIRM"):
        Broker(env="live")


def test_live_with_confirm_constructs(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    monkeypatch.setenv("ALPACA_LIVE_CONFIRM", "yes")
    b = Broker(env="live")
    assert b.env == "live"


def test_missing_keys_raises(monkeypatch):
    with pytest.raises(ConfigError, match="ALPACA_API_KEY"):
        Broker(env="paper")


def test_bad_env_raises():
    with pytest.raises(ConfigError, match="env"):
        Broker(env="demo")
