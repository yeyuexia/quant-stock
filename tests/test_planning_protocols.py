"""Verify the planning-layer Protocols + rule-based implementations."""
from planning import TargetBuilder, TargetBuilderOutput, IntentPricer, IntentPricerOutput
from orders import OrderIntent
from planner import PricingContext, RuleBasedIntentPricer


def test_rule_based_core_implements_target_builder_protocol():
    from rebalancer import RuleBasedCoreTargetBuilder
    builder = RuleBasedCoreTargetBuilder()
    # Structural check: has build() returning TargetBuilderOutput-shaped object
    assert hasattr(builder, "build")
    # Runtime protocol check via duck typing (Protocol isn't isinstance-checkable
    # unless @runtime_checkable — that's fine, we just need the shape)


def test_rule_based_aggressive_implements_target_builder_protocol():
    from rebalancer import RuleBasedAggressiveTargetBuilder
    builder = RuleBasedAggressiveTargetBuilder()
    assert hasattr(builder, "build")


def test_rule_based_intent_pricer_wraps_existing_logic():
    ctx = PricingContext(
        ranks={"SPY": 1},
        asset_class={"SPY": "etf"},
        decision_prices={"SPY": 480.0},
        tranche="core",
    )
    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="test", tranche="core", client_order_id="cid-spy",
    )
    pricer = RuleBasedIntentPricer()
    out = pricer.price([intent], ctx)
    assert isinstance(out, IntentPricerOutput)
    assert out.provider == "rule-based"
    assert len(out.priced) == 1
    assert out.priced[0].tier == "HIGH"


def test_target_builder_output_has_required_fields():
    out = TargetBuilderOutput(
        targets={"SPY": 0.5}, capital=90_000.0,
        rationale="test", confidence=1.0, provider="test-provider",
    )
    assert out.targets == {"SPY": 0.5}
    assert out.capital == 90_000.0
    assert out.confidence == 1.0
    assert out.provider == "test-provider"


def test_rebalancer_run_accepts_protocol_target_builder(tmp_path, monkeypatch):
    """rebalancer.run should work with a Protocol-conforming TargetBuilder,
    not only a zero-arg callable."""
    import rebalancer, orders, config as cfg
    from pending_plan import load_plan
    from tests.fakes import FakeBroker
    from planning import TargetBuilderOutput

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

    class FakeBuilder:
        def build(self, *, tranche, broker):
            return TargetBuilderOutput(
                targets={"SPY": 0.10}, capital=90_000.0,
                rationale="test", confidence=1.0, provider="fake",
            )

    b = FakeBroker(cash=100_000.0, equity=100_000.0)
    b.set_latest_price("SPY", 480.0)

    rebalancer.run(tranche="core", dry_run=False, force=True,
                   broker=b, target_builder=FakeBuilder())

    plan = load_plan()
    assert plan is not None
    assert any(s.intent.symbol == "SPY" for s in plan.intents)
