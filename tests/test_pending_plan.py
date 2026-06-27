# tests/test_pending_plan.py
import datetime as dt
from quant.execution.pending_plan import (
    PendingPlan, IntentState, Baseline, write_plan, load_plan, clear_plan,
)
from quant.execution.orders import OrderIntent
from unittest.mock import patch
import json
import os
import quant.execution.pending_plan as pending_plan
import pytest
import sys


def _sample_intent(symbol="SPY", notional=1000.0):
    return OrderIntent(
        symbol=symbol, notional=notional, side="buy",
        reason="test", tranche="core",
        client_order_id=f"cid-{symbol}",
        tier="MED", decision_price=480.0, max_price=481.5, slice_count=4,
    )


def test_write_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    baseline = Baseline(
        spy=480.0, vix=14.0, macro_score=0.12,
        news_cursor_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
    )
    plan = PendingPlan(
        plan_id="core-2026-04-17",
        tranche="core",
        created_at=dt.datetime(2026, 4, 17, 13, 35, tzinfo=dt.timezone.utc),
        baseline=baseline,
        intents=[IntentState(intent=_sample_intent())],
    )
    write_plan(plan)
    loaded = load_plan()
    assert loaded is not None
    assert loaded.plan_id == "core-2026-04-17"
    assert loaded.baseline.spy == 480.0
    assert loaded.intents[0].intent.symbol == "SPY"
    assert loaded.intents[0].status == "active"


def test_load_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "missing.json"))
    assert load_plan() is None


def test_clear_removes_file(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.execution.pending_plan.PENDING_PLAN_PATH", str(tmp_path / "plan.json"))
    baseline = Baseline(spy=480.0, vix=14.0, macro_score=0.0,
                        news_cursor_at=dt.datetime(2026, 4, 17, tzinfo=dt.timezone.utc))
    plan = PendingPlan(plan_id="t", tranche="core", created_at=baseline.news_cursor_at,
                       baseline=baseline, intents=[])
    write_plan(plan)
    clear_plan()
    assert load_plan() is None


def test_intent_state_defaults():
    s = IntentState(intent=_sample_intent())
    assert s.status == "active"
    assert s.notional_filled == 0.0
    assert s.slices_submitted == 0
    assert s.last_client_order_id is None
    assert s.abort_reason is None


# ======================================================================
# Post-review additions (formerly test_pending_plan_optimizations.py)
# ======================================================================

"""Regression tests for pending_plan.py hardening — atomic writes,
corrupt-file recovery, schema backward compat."""
import datetime as dt
import json
import os
import sys
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import quant.execution.pending_plan as pending_plan
from quant.execution.pending_plan import (
    PendingPlan, IntentState, Baseline, write_plan, load_plan, clear_plan,
)
from quant.execution.orders import OrderIntent


def _plan(tmp_path, monkeypatch):
    """Build a minimal valid plan + redirect path to tmp."""
    path = tmp_path / "p.json"
    monkeypatch.setattr(pending_plan, "PENDING_PLAN_PATH", str(path))
    intent = OrderIntent(
        symbol="SPY", notional=1000.0, side="buy",
        reason="t", tranche="core", client_order_id="cid-spy",
    )
    return PendingPlan(
        plan_id="test",
        tranche="core",
        created_at=dt.datetime(2026, 5, 24, 13, 0, tzinfo=dt.timezone.utc),
        baseline=Baseline(
            spy=480.0, vix=14.0, macro_score=0.2,
            news_cursor_at=dt.datetime(2026, 5, 24, 13, 0, tzinfo=dt.timezone.utc),
        ),
        intents=[IntentState(intent=intent)],
    )


def test_write_plan_creates_lock_sidecar(tmp_path, monkeypatch):
    """atomic_write_json creates a `.lock` sidecar."""
    plan = _plan(tmp_path, monkeypatch)
    write_plan(plan)
    assert (tmp_path / "p.json").exists()
    assert (tmp_path / "p.json.lock").exists()


def test_load_plan_returns_none_on_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_plan, "PENDING_PLAN_PATH",
                        str(tmp_path / "nope.json"))
    assert load_plan() is None


def test_load_plan_recovers_from_corrupt_json(tmp_path, monkeypatch, caplog):
    import logging
    path = tmp_path / "p.json"
    path.write_text("not valid json {{")
    monkeypatch.setattr(pending_plan, "PENDING_PLAN_PATH", str(path))
    with caplog.at_level(logging.WARNING, logger="pending_plan"):
        result = load_plan()
    assert result is None
    assert any("unreadable" in r.message for r in caplog.records)


def test_load_plan_recovers_from_schema_mismatch(tmp_path, monkeypatch, caplog):
    """If the saved plan is missing required fields, log + return None."""
    import logging
    path = tmp_path / "p.json"
    # Valid JSON but wrong shape (missing baseline / intents)
    path.write_text(json.dumps({"plan_id": "broken"}))
    monkeypatch.setattr(pending_plan, "PENDING_PLAN_PATH", str(path))
    with caplog.at_level(logging.WARNING, logger="pending_plan"):
        result = load_plan()
    assert result is None
    assert any("schema mismatch" in r.message for r in caplog.records)


def test_load_plan_tolerates_unknown_intent_field(tmp_path, monkeypatch):
    """A plan written by a newer/older OrderIntent schema with an extra
    field must still load (the extra field is silently dropped)."""
    plan = _plan(tmp_path, monkeypatch)
    write_plan(plan)

    # Inject a future field into the saved intent
    with open(tmp_path / "p.json") as f:
        data = json.load(f)
    data["intents"][0]["intent"]["future_field"] = "unknown"
    with open(tmp_path / "p.json", "w") as f:
        json.dump(data, f)

    loaded = load_plan()
    assert loaded is not None
    assert loaded.intents[0].intent.symbol == "SPY"


def test_roundtrip_preserves_state(tmp_path, monkeypatch):
    plan = _plan(tmp_path, monkeypatch)
    plan.breakers_tripped = ["A", "C:NVDA"]
    plan.news_hits_seen = {"abc123": "2026-05-24T13:30:00+00:00"}
    plan.intents[0].notional_filled = 500.0
    plan.intents[0].slices_submitted = 1
    plan.intents[0].status = "active"

    write_plan(plan)
    loaded = load_plan()

    assert loaded.breakers_tripped == ["A", "C:NVDA"]
    assert loaded.news_hits_seen == {"abc123": "2026-05-24T13:30:00+00:00"}
    assert loaded.intents[0].notional_filled == 500.0
    assert loaded.intents[0].slices_submitted == 1


def test_clear_plan_idempotent_on_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pending_plan, "PENDING_PLAN_PATH",
                        str(tmp_path / "nope.json"))
    clear_plan()  # must not raise
