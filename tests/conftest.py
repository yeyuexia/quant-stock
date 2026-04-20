"""Shared pytest fixtures — auto-applied to every test in tests/."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_telegram_notify_path(tmp_path, monkeypatch):
    """Redirect TELEGRAM_NOTIFY_PATH to a per-test tmp file so real cache
    at .cache/telegram_notifications.json is never written during tests."""
    import config
    monkeypatch.setattr(
        config, "TELEGRAM_NOTIFY_PATH",
        str(tmp_path / "_isolated_telegram_notifications.json"),
    )
