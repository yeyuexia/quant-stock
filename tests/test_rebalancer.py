"""rebalancer.py — end-to-end wiring tests with FakeBroker."""
import datetime as dt
import json
import pytest
from tests.fakes import FakeBroker


def _portfolio_cache(tmp_path, monkeypatch, data):
    monkeypatch.setattr("quant.execution.orders.PORTFOLIO_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr("quant.execution.orders.DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    if data is not None:
        (tmp_path / "portfolio.json").write_text(json.dumps(data))


def _safety_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.execution.orders.HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr("quant.execution.orders.DAILY_TRADE_LOG", str(tmp_path / "daily_trade_log.json"))
    monkeypatch.setattr("quant.execution.orders.PENDING_ORDERS_PATH", str(tmp_path / "pending_orders.json"))


def test_rebalancer_dry_run_no_submits(tmp_path, monkeypatch, capsys):
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    from quant.execution.rebalancer import run

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
    from quant.execution.rebalancer import run

    fb = FakeBroker()
    submitted = run(tranche="core", dry_run=False, force=False, broker=fb,
                     target_builder=lambda: ({"SPY": 1.0}, 10_000))
    assert submitted is None   # skipped


def test_rebalancer_submits_when_forced(tmp_path, monkeypatch):
    """Large orders (>= $500 threshold) now go to pending_plan, not direct-submit."""
    from quant.execution.rebalancer import run
    _portfolio_cache(tmp_path, monkeypatch, None)
    _safety_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("quant.execution.orders.LARGE_ORDER_THRESHOLD", 100_000)
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    import quant.config as cfg
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import quant.signals.baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    fb = FakeBroker()
    fb.set_latest_price("SPY", 480.0)
    result = run(tranche="core", dry_run=False, force=True, broker=fb,
                  target_builder=lambda: ({"SPY": 1.0}, 10_000))
    # $10k SPY order >= $500 threshold → goes to pending_plan, not direct-submit
    assert len(result.submitted) == 0
    from quant.execution.pending_plan import load_plan
    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "SPY" for s in plan.intents)


def test_rebalancer_writes_pending_plan_for_large_orders(tmp_path, monkeypatch):
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    b = FakeBroker(cash=50_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)

    def fake_target_builder():
        return {"SPY": 0.20}, 90_000.0

    import quant.signals.baseline as bl
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))

    b = FakeBroker()
    b.set_latest_price("SPY", 480.0)

    def fake_target_builder():
        return {"SPY": 0.003}, 100_000.0   # 0.3% × 100k = $300, below $500 threshold

    import quant.signals.baseline as bl
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    notify_path = tmp_path / "telegram_notifications.json"
    monkeypatch.setattr(cfg, "TELEGRAM_NOTIFY_PATH", str(notify_path))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    b = FakeBroker(cash=50_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)

    import quant.signals.baseline as bl
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import quant.signals.baseline as bl
    monkeypatch.setattr(bl, "_fetch_spy", lambda: 480.0)
    monkeypatch.setattr(bl, "_fetch_vix", lambda: 14.0)
    monkeypatch.setattr(bl, "_fetch_macro_score", lambda: 0.0)

    # default_price=None restores the original strict behavior (raise when
    # a symbol isn't seeded) — this test relies on NVDA's missing price to
    # exercise the drop-symbol path.
    b = FakeBroker(cash=50_000.0, equity=100_000.0, default_price=None)
    b.set_latest_price("SPY", 480.0)
    # Deliberately DO NOT seed NVDA price — FakeBroker.latest_price will raise

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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import quant.signals.baseline as bl
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import quant.signals.baseline as bl
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
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    from quant.execution.pending_plan import load_plan
    from tests.fakes import FakeBroker

    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "no_halt"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pend.json"))
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr(cfg, "EXECUTOR_SHADOW_MODE", False)

    import quant.signals.baseline as bl
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


def test_rebalancer_writes_entry_pivots_for_screener_picks(tmp_path, monkeypatch):
    """A screener pick that isn't currently held should get an entry_pivots record."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import datetime as dt
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Stub screener to return one stock pick with a base_hi.
    df = pd.DataFrame([{
        "ticker": "NVDA", "price": 150.0, "rs_score": 90.0, "adr": 0.05,
        "above_ema_fast": True, "above_ema_slow": True,
        "in_base": True, "base_weeks": 8, "base_depth": 0.10,
        "base_tightness": 0.03, "base_hi": 148.0,
    }])
    monkeypatch.setattr("quant.signals.screener.screen_stocks", lambda: df)
    # Stub momentum + macro so they don't fetch network data.
    monkeypatch.setattr("quant.signals.momentum.generate_signals", lambda **kw: {"holdings": [], "holdings_ranked": []})
    monkeypatch.setattr("quant.signals.macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets(90_000)

    pivots = orders._load_entry_pivots()
    assert "NVDA" in pivots
    assert pivots["NVDA"]["pivot"] == 148.0
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert pivots["NVDA"]["entry_date"] == today


def test_rebalancer_skips_entry_pivots_for_already_held_screener_picks(tmp_path, monkeypatch):
    """If the symbol is already in the portfolio cache, don't refresh its pivot."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # Pre-seed portfolio cache showing NVDA already held.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "2026-05-10T14:00:00+00:00", "alpaca_env": "paper",
        "cash": 0.0, "equity": 0.0,
        "positions": [{"symbol": "NVDA", "shares": 10, "avg_entry": 150.0,
                       "market_value": 1500.0, "unrealized_pl": 0.0,
                       "tranche": "core", "entry_reason": "core rebalance"}],
        "tranches": {"core": {"last_rebalance": "2026-05-10"},
                     "aggressive": {"last_rebalance": None}},
    }))
    # Pre-seed an older pivot record so we can detect overwrite.
    (tmp_path / "pivots.json").write_text(json.dumps({
        "NVDA": {"pivot": 140.0, "entry_date": "2026-05-01"}
    }))

    df = pd.DataFrame([{
        "ticker": "NVDA", "price": 150.0, "rs_score": 90.0, "adr": 0.05,
        "above_ema_fast": True, "above_ema_slow": True,
        "in_base": True, "base_weeks": 8, "base_depth": 0.10,
        "base_tightness": 0.03, "base_hi": 148.0,
    }])
    monkeypatch.setattr("quant.signals.screener.screen_stocks", lambda: df)
    monkeypatch.setattr("quant.signals.momentum.generate_signals", lambda **kw: {"holdings": [], "holdings_ranked": []})
    monkeypatch.setattr("quant.signals.macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets(90_000)

    pivots = orders._load_entry_pivots()
    # The pre-existing pivot record is preserved (no refresh on already-held).
    assert pivots["NVDA"]["pivot"] == 140.0
    assert pivots["NVDA"]["entry_date"] == "2026-05-01"


def test_rebalancer_skips_entry_pivots_for_etf_targets(tmp_path, monkeypatch):
    """ETF entries from momentum (no screener row) get no pivot record."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    monkeypatch.setattr("quant.signals.screener.screen_stocks", lambda: pd.DataFrame())  # empty
    monkeypatch.setattr("quant.signals.momentum.generate_signals",
                        lambda **kw: {"holdings": [("SPY", 1.0)], "holdings_ranked": [("SPY", 1.0, 1)]})
    monkeypatch.setattr("quant.signals.macro.macro_risk_adjustment", lambda x: 1.0)

    rebalancer._build_core_targets(90_000)
    pivots = orders._load_entry_pivots()
    assert "SPY" not in pivots


# ── New rebalancer optimizations: empty/sparse sleeve, pivot fallback,
#    dynamic tranche_capital, daily cadence ─────────────────────────

def _stub_core_inputs(monkeypatch, *, screener_df=None,
                      etf_holdings=(("SPY", 1.0),), macro_adj=1.0):
    """Common stubs for _build_core_targets so it doesn't touch network."""
    import pandas as pd
    if screener_df is None:
        screener_df = pd.DataFrame()
    monkeypatch.setattr("quant.signals.screener.screen_stocks", lambda: screener_df)
    monkeypatch.setattr("quant.signals.momentum.generate_signals", lambda **kw: {
        "holdings": list(etf_holdings),
        "holdings_ranked": [(s, w, i + 1) for i, (s, w) in enumerate(etf_holdings)],
    })
    monkeypatch.setattr("quant.signals.macro.macro_risk_adjustment", lambda x: macro_adj)


def test_empty_screener_rolls_stock_pct_to_bil(tmp_path, monkeypatch):
    """When screener returns no picks, the stock_pct allocation must flow to BIL,
    not silently sit in cash for 30 days."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ETF_ALLOCATION_PCT", 0.80)
    monkeypatch.setattr(cfg, "STOCK_ALLOCATION_PCT", 0.20)
    monkeypatch.setattr(cfg, "CASH_BUFFER_PCT", 0.05)
    _stub_core_inputs(monkeypatch, screener_df=None, etf_holdings=(("SPY", 1.0),))

    targets, _ = rebalancer._build_core_targets(90_000)
    # SPY gets 0.80; stock_pct (0.20) was unallocated → must flow to BIL.
    assert abs(targets["SPY"] - 0.80) < 1e-6
    assert targets.get(cfg.SAFE_HAVEN, 0) >= 0.20 - 1e-6
    # Total deployed = 1.0 (cash buffer absorbed in healthy regime — by design).
    assert abs(sum(targets.values()) - 1.0) < 1e-6


def test_sparse_screener_caps_per_stock_at_max_position_pct(tmp_path, monkeypatch):
    """When screener returns only 1 stock, that stock must not get the whole
    stock_pct — cap at MAX_POSITION_PCT and roll the rest to BIL."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ETF_ALLOCATION_PCT", 0.50)
    monkeypatch.setattr(cfg, "STOCK_ALLOCATION_PCT", 0.50)
    monkeypatch.setattr(cfg, "MAX_POSITION_PCT", 0.10)  # cap at 10%
    monkeypatch.setattr(cfg, "STOCK_SLEEVE_TOP_N", 3)
    monkeypatch.setattr(cfg, "CASH_BUFFER_PCT", 0.05)

    df = pd.DataFrame([{
        "ticker": "NVDA", "price": 200.0, "base_hi": 198.0,
    }])
    _stub_core_inputs(monkeypatch, screener_df=df, etf_holdings=())

    targets, _ = rebalancer._build_core_targets(90_000)
    # NVDA gets exactly 10% (capped), not the full 50% stock_pct
    assert abs(targets["NVDA"] - 0.10) < 1e-6
    # Remainder (0.40) of stock_pct rolls to BIL
    assert targets.get(cfg.SAFE_HAVEN, 0) >= 0.40 - 1e-6


def test_pivot_fallback_to_price_when_base_hi_missing(tmp_path, monkeypatch):
    """When a screener pick has no VCP base_hi, use the screening price as
    the pivot so SEPA failed-breakout always has a reference."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import pandas as pd
    import numpy as np
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    df = pd.DataFrame([{
        "ticker": "AAPL", "price": 175.50, "base_hi": np.nan,  # NO clean base
    }])
    _stub_core_inputs(monkeypatch, screener_df=df)

    rebalancer._build_core_targets(90_000)

    pivots = orders._load_entry_pivots()
    assert "AAPL" in pivots
    assert pivots["AAPL"]["pivot"] == 175.50  # fell back to current price


def test_pivot_prefers_base_hi_over_price_when_available(tmp_path, monkeypatch):
    """base_hi takes precedence over price when both are present."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    df = pd.DataFrame([{
        "ticker": "AAPL", "price": 175.50, "base_hi": 172.00,
    }])
    _stub_core_inputs(monkeypatch, screener_df=df)
    rebalancer._build_core_targets(90_000)

    pivots = orders._load_entry_pivots()
    assert pivots["AAPL"]["pivot"] == 172.00  # base_hi wins


def test_held_filter_only_considers_core_tranche(tmp_path, monkeypatch):
    """A symbol held in OTHER tranches should still get a fresh pivot when
    selected by core's screener — only core-held symbols are skipped."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import pandas as pd
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))

    # AAPL is held but in aggressive/unknown — not core.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper", "cash": 0.0, "equity": 0.0,
        "positions": [{"symbol": "AAPL", "shares": 1, "avg_entry": 100,
                       "market_value": 100, "unrealized_pl": 0,
                       "tranche": "unknown", "entry_reason": "manual"}],
        "tranches": {},
    }))

    df = pd.DataFrame([{
        "ticker": "AAPL", "price": 175.50, "base_hi": 172.00,
    }])
    _stub_core_inputs(monkeypatch, screener_df=df)
    rebalancer._build_core_targets(90_000)

    pivots = orders._load_entry_pivots()
    # AAPL should get a fresh pivot — it's not held in CORE.
    assert pivots["AAPL"]["pivot"] == 172.00


def test_run_uses_dynamic_tranche_capital_from_snap(tmp_path, monkeypatch):
    """When account equity = $150K, core tranche_capital should be ~$135K
    (90% of system equity), not the static $90K from INITIAL_CAPITAL."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "trade_log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(cfg, "AGGRESSIVE_TRANCHE_PCT", 0.10)
    monkeypatch.setattr(cfg, "INITIAL_CAPITAL", 100_000)

    captured = {}

    class CapturingBuilder:
        def build(self, *, tranche, broker, tranche_capital):
            from quant.execution.planning import TargetBuilderOutput
            captured["capital"] = tranche_capital
            return TargetBuilderOutput(
                targets={"BIL": 0.50}, capital=tranche_capital,
                rationale="x", confidence=1.0, provider="capturing")

    fb = FakeBroker(cash=150_000.0, equity=150_000.0)
    rebalancer.run(tranche="core", dry_run=True, force=True,
                   broker=fb, target_builder=CapturingBuilder())

    # 150_000 × 0.90 = 135_000
    assert abs(captured["capital"] - 135_000.0) < 1.0


def test_run_subtracts_unknown_tranche_from_system_equity(tmp_path, monkeypatch):
    """Unknown-tranche positions don't count toward the budget core/aggressive
    allocate against — they're held outside the system's control."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "trade_log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(cfg, "AGGRESSIVE_TRANCHE_PCT", 0.10)
    # Adoption is self-healing and on by default, which would reclassify the
    # 'unknown' seed into a sleeve. This test targets the _system_equity
    # subtraction safety net, which is what guards the flag-OFF case — so keep
    # adoption off here to preserve a genuine 'unknown' position.
    monkeypatch.setattr(cfg, "ADOPT_EXTERNAL_POSITIONS", False)
    # Pre-seed unknown position worth $20K.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper",
        "cash": 80_000.0, "equity": 100_000.0,
        "positions": [{"symbol": "MANUAL", "shares": 10, "avg_entry": 2000,
                       "market_value": 20_000.0, "unrealized_pl": 0,
                       "tranche": "unknown", "entry_reason": "manual"}],
        "tranches": {},
    }))

    captured = {}

    class CapturingBuilder:
        def build(self, *, tranche, broker, tranche_capital):
            from quant.execution.planning import TargetBuilderOutput
            captured["capital"] = tranche_capital
            return TargetBuilderOutput(
                targets={}, capital=tranche_capital,
                rationale="x", confidence=1.0, provider="capturing")

    # FakeBroker carries the on-disk snapshot's equity; sync_state will rewrite
    # portfolio cache from the live account but preserve unknown tagging.
    fb = FakeBroker(cash=80_000.0, equity=100_000.0)
    fb.seed_position("MANUAL", qty=10, avg_entry=2000, mv=20_000.0)
    rebalancer.run(tranche="core", dry_run=True, force=True,
                   broker=fb, target_builder=CapturingBuilder())

    # System equity = 100_000 - 20_000 (unknown) = 80_000
    # Core capital = 80_000 × 0.90 = 72_000
    assert abs(captured["capital"] - 72_000.0) < 1.0


def test_daily_cadence_lets_run_proceed_one_day_after_last(tmp_path, monkeypatch):
    """REBALANCE_DAYS=1 means yesterday's rebalance does NOT block today's."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import datetime as _dt
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "trade_log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(cfg, "REBALANCE_DAYS", {"core": 1, "aggressive": 1})

    # Seed: last rebalance was YESTERDAY.
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper",
        "cash": 100_000.0, "equity": 100_000.0, "positions": [],
        "tranches": {"core": {"last_rebalance": yesterday}},
    }))

    fb = FakeBroker(cash=100_000.0, equity=100_000.0)
    result = rebalancer.run(tranche="core", dry_run=True, force=False, broker=fb,
                            target_builder=lambda: ({}, 90_000))
    assert result is not None  # would be None if cadence had blocked


def test_aggressive_hysteresis_keeps_held_etf_on_one_rank_slip(tmp_path, monkeypatch):
    """Aggressive: a held leveraged ETF slipped from rank 2 to rank 3 is
    kept (hysteresis_depth=1)."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import pandas as pd

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "AGGRESSIVE_PARAMS", {
        **cfg.AGGRESSIVE_PARAMS,
        "momentum_top_n": 2,
        "hysteresis_depth": 1,
    })
    monkeypatch.setattr(cfg, "_ETF_LEVERAGED", ["TQQQ", "SOXL", "UPRO"])
    monkeypatch.setattr(cfg, "SMA_FILTER_PERIOD", 5)

    # Held: SOXL. Today's ranking would be TQQQ=1, UPRO=2, SOXL=3 → without
    # hysteresis SOXL gets sold.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper", "cash": 0.0, "equity": 0.0,
        "positions": [{"symbol": "SOXL", "shares": 1, "avg_entry": 100,
                       "market_value": 100, "unrealized_pl": 0,
                       "tranche": "aggressive", "entry_reason": "x"}],
        "tranches": {},
    }))

    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    # All three above their 200SMA; scores designed to rank TQQQ > UPRO > SOXL.
    import numpy as np
    prices = pd.DataFrame({
        "TQQQ": 100 * np.cumprod(1 + np.full(300, 0.003)),   # +0.3%/day → fastest
        "UPRO": 100 * np.cumprod(1 + np.full(300, 0.002)),   # +0.2%/day → mid
        "SOXL": 100 * np.cumprod(1 + np.full(300, 0.0015)),  # +0.15%/day → slowest
        "BIL":  100 * np.cumprod(1 + np.full(300, 0.0001)),  # ~flat (safe haven)
    }, index=idx)
    monkeypatch.setattr("quant.data.market.fetch_prices", lambda *a, **kw: prices)

    targets, _ = rebalancer._build_aggressive_targets(10_000)
    # Top-2 = {TQQQ, UPRO}; SOXL is rank 3 but held → sticky → included.
    held_in_targets = {sym for sym in targets if sym in ("TQQQ", "UPRO", "SOXL")}
    assert held_in_targets == {"TQQQ", "UPRO", "SOXL"}


def test_aggressive_hysteresis_drops_unheld_at_rank_three(tmp_path, monkeypatch):
    """Aggressive: an UNHELD leveraged ETF at rank 3 is NOT sticky."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import pandas as pd
    import numpy as np

    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "ENTRY_PIVOTS_PATH", str(tmp_path / "pivots.json"))
    monkeypatch.setattr(cfg, "AGGRESSIVE_PARAMS", {
        **cfg.AGGRESSIVE_PARAMS,
        "momentum_top_n": 2,
        "hysteresis_depth": 1,
    })
    monkeypatch.setattr(cfg, "_ETF_LEVERAGED", ["TQQQ", "SOXL", "UPRO"])
    monkeypatch.setattr(cfg, "SMA_FILTER_PERIOD", 5)

    # Empty portfolio — no held ETFs at all.
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper", "cash": 10_000.0, "equity": 10_000.0,
        "positions": [], "tranches": {},
    }))

    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    prices = pd.DataFrame({
        "TQQQ": 100 * np.cumprod(1 + np.full(300, 0.003)),
        "UPRO": 100 * np.cumprod(1 + np.full(300, 0.002)),
        "SOXL": 100 * np.cumprod(1 + np.full(300, 0.0015)),
        "BIL":  100 * np.cumprod(1 + np.full(300, 0.0001)),
    }, index=idx)
    monkeypatch.setattr("quant.data.market.fetch_prices", lambda *a, **kw: prices)

    targets, _ = rebalancer._build_aggressive_targets(10_000)
    leveraged_in_targets = {sym for sym in targets if sym in ("TQQQ", "UPRO", "SOXL")}
    # Only top-2; SOXL is rank 3 but not held → dropped.
    assert leveraged_in_targets == {"TQQQ", "UPRO"}


def test_daily_cadence_still_blocks_same_day_rerun(tmp_path, monkeypatch):
    """Even with daily cadence, running twice on the same day is blocked."""
    import quant.execution.rebalancer as rebalancer, quant.execution.orders as orders, quant.config as cfg
    import json
    import datetime as _dt
    monkeypatch.setattr(orders, "PORTFOLIO_PATH", str(tmp_path / "port.json"))
    monkeypatch.setattr(orders, "DAILY_LOG_PATH", str(tmp_path / "daily_log.csv"))
    monkeypatch.setattr(orders, "HALT_PATH", str(tmp_path / "HALT"))
    monkeypatch.setattr(orders, "DAILY_TRADE_LOG", str(tmp_path / "trade_log.json"))
    monkeypatch.setattr(orders, "PENDING_ORDERS_PATH", str(tmp_path / "pending.json"))
    monkeypatch.setattr(cfg, "REBALANCE_DAYS", {"core": 1, "aggressive": 1})

    today = _dt.date.today().isoformat()
    (tmp_path / "port.json").write_text(json.dumps({
        "synced_at": "x", "alpaca_env": "paper",
        "cash": 100_000.0, "equity": 100_000.0, "positions": [],
        "tranches": {"core": {"last_rebalance": today}},
    }))

    fb = FakeBroker(cash=100_000.0, equity=100_000.0)
    result = rebalancer.run(tranche="core", dry_run=True, force=False, broker=fb,
                            target_builder=lambda: ({}, 90_000))
    assert result is None  # same-day re-run blocked (0 < 1)
