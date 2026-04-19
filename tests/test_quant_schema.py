import datetime as dt
import json
from dataclasses import asdict


def test_external_signal_roundtrip():
    from quant.schema import ExternalSignal
    s = ExternalSignal(
        source="13F",
        as_of=dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc),
        data=[{"fund": "Berkshire", "ticker": "AAPL", "weight": 0.23}],
        error=None,
    )
    d = asdict(s)
    d["as_of"] = s.as_of.isoformat()
    blob = json.dumps(d)
    back = json.loads(blob)
    assert back["source"] == "13F"
    assert back["data"][0]["ticker"] == "AAPL"
    assert back["error"] is None


def test_external_signal_error_case():
    from quant.schema import ExternalSignal
    s = ExternalSignal(
        source="reddit",
        as_of=dt.datetime(2026, 4, 19, tzinfo=dt.timezone.utc),
        data=[],
        error="connection refused",
    )
    assert s.data == []
    assert s.error == "connection refused"


def test_proposed_change_fields():
    from quant.schema import ProposedChange
    c = ProposedChange(
        key="STOP_LOSS_PCT",
        current_value=0.08,
        proposed_value=0.075,
        rationale="ATR compressed 30%",
        detailed_plan="Next rebalance attaches 7.5% stops",
        expected_effect="cuts losers 15% faster",
        risk_tier="low",
        confidence=0.70,
    )
    assert c.risk_tier == "low"
    assert c.confidence == 0.70


def test_proposed_change_rejects_invalid_risk_tier():
    """risk_tier is validated by the applier, not the dataclass itself."""
    from quant.schema import ProposedChange
    c = ProposedChange(
        key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075,
        rationale="r", detailed_plan="p", expected_effect="e",
        risk_tier="medium",
        confidence=0.5,
    )
    assert c.risk_tier == "medium"


def test_quant_review_requires_no_changes_reason_when_empty():
    from quant.schema import QuantReview
    r = QuantReview(
        date="2026-04-19",
        portfolio_summary="baseline",
        macro_read="risk-on",
        reasoning_summary="nothing new",
        data_gaps=[],
        proposed_changes=[],
        no_changes_reason="all signals confirm current strategy",
    )
    assert r.proposed_changes == []
    assert r.no_changes_reason is not None


def test_applier_result_defaults():
    from quant.schema import ApplierResult
    r = ApplierResult()
    assert r.applied_low == []
    assert r.queued_high == []
    assert r.rejected_forbidden == []
    assert r.rejected_out_of_bounds == []
    assert r.rejected_malformed == []
