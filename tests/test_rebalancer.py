"""rebalancer.py — end-to-end wiring tests with FakeBroker."""
import datetime as dt
import json
import pytest
from tests.fakes import FakeBroker


def _portfolio_cache(tmp_path, monkeypatch, data):
    monkeypatch.setattr("orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def _safety_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))


def test_rebalancer_dry_run_no_submits(tmp_path, monkeypatch, capsys):
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    from rebalancer import run

    fb = FakeBroker()
    run(tranche="core", dry_run=True, force=True, broker=fb,
        target_builder=lambda: ({"SPY": 1.0}, 10_000))
    out = capsys.readouterr().out
    assert "SPY" in out
    assert fb._submitted == []


def test_rebalancer_skips_when_not_due(tmp_path, monkeypatch):
    _portfolio_cache(tmp_path, monkeypatch, {
        "synced_at": "x", "alpaca_env": "paper", "cash": 0, "equity": 0,
        "positions": [],
        "tranches": {"core": {"last_rebalance": dt.date.today().isoformat()},
                     "aggressive": {"last_rebalance": None}},
    })
    _safety_paths(tmp_path, monkeypatch)
    from rebalancer import run

    fb = FakeBroker()
    submitted = run(tranche="core", dry_run=False, force=False, broker=fb,
                     target_builder=lambda: ({"SPY": 1.0}, 10_000))
    assert submitted is None   # skipped


def test_rebalancer_submits_when_forced(tmp_path, monkeypatch):
    """Large orders (>= $500 threshold) now go to pending_plan, not direct-submit."""
    from rebalancer import run
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("orders.LARGE_ORDER_THRESHOLD", 100_000)
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    import config as cfg
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    fb = FakeBroker()
    fb.set_latest_price("SPY", 480.0)
    result = run(tranche="core", dry_run=False, force=True, broker=fb,
                  target_builder=lambda: ({"SPY": 1.0}, 10_000))
    # $10k SPY order >= $500 threshold → goes to pending_plan, not direct-submit
    assert len(result.submitted) == 0
    from pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "SPY" for s in plan.intents)


def test_rebalancer_writes_pending_plan_for_large_orders(tmp_path, monkeypatch):
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    b = FakeBroker(cash=50_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)

    def fake_target_builder():
        return {"SPY": 0.20}, 90_000.0

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.12)

    rebalancer.run(tranche="core", dry_run=False, force=True,
                   broker=b, target_builder=fake_target_builder)

    plan = load_plan()
    assert plan is not None
    assert plan.tranche == "core"
    assert any(s.intent.symbol == "SPY" for s in plan.intents)
    # SPY intent is $18K, well above direct-submit threshold → in plan, not market-submitted
    assert len(b._submitted) == 0


def test_rebalancer_direct_submits_tiny_orders(tmp_path, monkeypatch):
    import rebalancer, orders, config as cfg
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))

    b = FakeBroker()
    b.set_latest_price("SPY", 480.0)

    def fake_target_builder():
        return {"SPY": 0.003}, 100_000.0   # 0.3% × 100k = $300, below $500 threshold

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    rebalancer.run(tranche="core", dry_run=False, force=True,
                   broker=b, target_builder=fake_target_builder)

    # Below threshold → submitted directly (market order via execute_plan)
    assert len(b._submitted) == 1


def test_rebalancer_writes_tg_notification_on_plan_write(tmp_path, monkeypatch):
    """After pending_plan.json is written, a Telegram notification with
    source='rebalancer' is appended to TELEGRAM_NOTIFY_PATH, summarising
    the tranche, capital, and buy/sell intents."""
    import rebalancer, orders, config as cfg
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    b = FakeBroker(cash=50_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.12)

    rebalancer.run(tranche="core", dry_run=False, force=True, broker=b,
                   target_builder=lambda: ({"SPY": 0.20}, 90_000.0))

    assert notify_path.exists(), "TG notification file should be created"
    notifications = json.loads(notify_path.read_text())
    rebalancer_notifs = [n for n in notifications if n.get("source") == "rebalancer"]
    assert len(rebalancer_notifs) == 1, \
        f"expected exactly 1 rebalancer notification, got {len(rebalancer_notifs)}"
    entry = rebalancer_notifs[0]
    assert "ts" in entry
    msg = entry["message"]
    assert "core" in msg.lower()
    assert "SPY" in msg


def test_rebalancer_skips_tg_notification_on_dry_run(tmp_path, monkeypatch):
    """Dry-run must not write a TG notification (no plan file is written)."""
    import rebalancer, orders, config as cfg
    from tests.fakes import FakeBroker

    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))

    b = FakeBroker()
    rebalancer.run(tranche="core", dry_run=True, force=True, broker=b,
                   target_builder=lambda: ({"SPY": 1.0}, 10_000))

    assert not notify_path.exists()


def test_rebalancer_drops_symbol_with_missing_decision_price(tmp_path, monkeypatch, capsys):
    """If _latest_price raises, the symbol should be dropped from the pending plan
    with a warning, not silently included with decision_price=0.0."""
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    b = FakeBroker(cash=50_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)
    # Deliberately DO NOT seed NVDA price — FakeBroker._latest_price will raise

    def fake_target_builder():
        return {"SPY": 0.10, "NVDA": 0.05}, 90_000.0

    rebalancer.run(tranche="core", dry_run=False, force=True,
                   broker=b, target_builder=fake_target_builder)

    plan = load_plan()
    # Plan must exist with SPY but NOT NVDA
    assert plan is not None
    syms = [s.intent.symbol for s in plan.intents]
    assert "SPY" in syms
    assert "NVDA" not in syms

    # And a warning should have been printed
    captured = capsys.readouterr()
    assert "NVDA" in captured.out
    assert "decision price" in captured.out.lower() or "price" in captured.out.lower()


def test_rebalancer_core_then_aggressive_preserves_both(tmp_path, monkeypatch):
    """Running --tranche core then --tranche aggressive should leave BOTH
    tranches' intents in pending_plan.json, not clobber the first."""
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    b = FakeBroker(cash=100_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)
    b.set_latest_price("TQQQ", 60.0)

    rebalancer.run(
        tranche="core", dry_run=False, force=True, broker=b,
        target_builder=lambda: ({"SPY": 0.20}, 90_000.0),
    )
    rebalancer.run(
        tranche="aggressive", dry_run=False, force=True, broker=b,
        target_builder=lambda: ({"TQQQ": 0.50}, 10_000.0),
    )

    plan = load_plan()
    assert plan is not None
    tranches_in_plan = {s.intent.tranche for s in plan.intents}
    assert tranches_in_plan == {"core", "aggressive"}
    symbols = {s.intent.symbol for s in plan.intents}
    assert {"SPY", "TQQQ"} <= symbols


def test_rebalancer_same_tranche_rerun_replaces_not_duplicates(tmp_path, monkeypatch):
    """Running --tranche core twice should replace core's intents, not duplicate them."""
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    b = FakeBroker(cash=100_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)
    b.set_latest_price("QQQ", 400.0)

    # First run: SPY
    rebalancer.run(
        tranche="core", dry_run=False, force=True, broker=b,
        target_builder=lambda: ({"SPY": 0.20}, 90_000.0),
    )
    # Second run (same tranche): QQQ instead
    rebalancer.run(
        tranche="core", dry_run=False, force=True, broker=b,
        target_builder=lambda: ({"QQQ": 0.20}, 90_000.0),
    )

    plan = load_plan()
    symbols = {s.intent.symbol for s in plan.intents}
    # Core's earlier SPY intent should be gone; replaced with QQQ.
    assert "SPY" not in symbols
    assert "QQQ" in symbols


def test_aggressive_tranche_picks_get_high_tier(tmp_path, monkeypatch):
    """Aggressive-tranche picks (leveraged ETFs) should be HIGH tier with
    wider tolerance, not default MED. They aren't in momentum.generate_signals'
    holdings_ranked, so _write_pending_plan must assign rank=1 directly."""
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    b = FakeBroker(cash=100_000.0, equity=100_000.0)
    b.set_latest_price("SOXL", 30.0)
    b.set_latest_price("LABU", 15.0)

    rebalancer.run(
        tranche="aggressive", dry_run=False, force=True, broker=b,
        target_builder=lambda: ({"SOXL": 0.50, "LABU": 0.50}, 10_000.0),
    )

    plan = load_plan()
    by_sym = {s.intent.symbol: s.intent for s in plan.intents}
    assert by_sym["SOXL"].tier == "HIGH"
    assert by_sym["LABU"].tier == "HIGH"
