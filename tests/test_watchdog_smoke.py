"""Smoke test: watchdog.snapshot() returns a PortfolioSnapshot via FakeBroker."""
import json
from tests.fakes import FakeBroker


def test_snapshot_returns_portfolio_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))

    # Patch broker construction to return FakeBroker
    fb = FakeBroker()
    fb.seed_position("SPY", 10, 500)
    monkeypatch.setattr("watchdog.Broker", lambda env: fb)

    from watchdog import snapshot
    snap = snapshot()
    assert snap.cash == 100_000
    assert any(p["symbol"] == "SPY" for p in snap.positions)
