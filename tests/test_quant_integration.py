"""End-to-end test of the quant review pipeline with a canned agent output.

No LLM call; we simulate what the agent would have written to
.cache/proposed_changes.json and verify the full apply → outputs flow."""
import json
import os
import subprocess
import sys


def test_end_to_end_pipeline(tmp_path):
    """Simulate one day's review end-to-end with canned inputs."""
    review = {
        "date": "2026-04-19",
        "portfolio_summary": "$100,250 equity; risk-on macro; 10 positions across tranches.",
        "macro_read": "risk-on at +0.45; VIX 17.7",
        "reasoning_summary": (
            "Macro firmly risk-on but 13F shows Q4 tech-trimming; Reddit NVDA "
            "sentiment bearish; Pelosi bought TSLA. Tighten risk controls slightly; "
            "add two accumulating names."
        ),
        "data_gaps": [],
        "proposed_changes": [
            {"key": "STOP_LOSS_PCT", "current_value": 0.08, "proposed_value": 0.075,
             "rationale": "10-day realized ATR compressed 30% — tighter matches vol regime.",
             "detailed_plan": "Next rebalance attaches 7.5% stops instead of 8%.",
             "expected_effect": "~15% earlier cut on losers.",
             "risk_tier": "low", "confidence": 0.70},
            {"key": "MOMENTUM_TOP_N", "current_value": 4, "proposed_value": 3,
             "rationale": "Top-1 XLK 3x rank-4 MTUM momentum; concentration helps in trends.",
             "detailed_plan": "Drop MTUM ~$15K, redistribute to rank 1-3.",
             "expected_effect": "top-3 concentration rises 60→75%.",
             "risk_tier": "high", "confidence": 0.65},
            {"key": "DAILY_MAX_NOTIONAL", "current_value": 25_000, "proposed_value": 100_000,
             "rationale": "Larger runway", "detailed_plan": "allow larger orders",
             "expected_effect": "more capacity", "risk_tier": "low", "confidence": 0.9},
        ],
        "no_changes_reason": None,
    }
    proposals_file = tmp_path / "proposed_changes.json"
    proposals_file.write_text(json.dumps(review, indent=2))

    overrides_file = tmp_path / "strategy_overrides.json"
    queue_file = tmp_path / "strategy_proposals.json"
    tg_file = tmp_path / "telegram_notifications.json"
    audit_file = tmp_path / "quant_review.log"

    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_file),
        "QUANT_APPLY_PROPOSALS_PATH": str(queue_file),
        "QUANT_APPLY_TG_PATH": str(tg_file),
        "QUANT_APPLY_AUDIT_PATH": str(audit_file),
    }

    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", str(proposals_file)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    # Low-risk written to overrides
    overrides = json.loads(overrides_file.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075

    # High-risk queued with id + expiry
    queue = json.loads(queue_file.read_text())
    assert len(queue) == 1
    assert queue[0]["key"] == "MOMENTUM_TOP_N"
    assert queue[0]["id"].startswith("prop_")
    assert "expires_at" in queue[0]

    # TG notification contains all three sections
    notifs = json.loads(tg_file.read_text())
    msg = notifs[-1]["message"]
    assert "AUTO-APPLIED" in msg
    assert "STOP_LOSS_PCT" in msg
    assert "NEEDS YOUR APPROVAL" in msg
    assert "MOMENTUM_TOP_N" in msg
    assert "REJECTED" in msg
    assert "DAILY_MAX_NOTIONAL" in msg

    # Audit log has one record
    assert audit_file.exists()
    lines = audit_file.read_text().strip().split("\n")
    record = json.loads(lines[-1])
    assert record["applied_low_count"] == 1
    assert record["queued_high_count"] == 1
    assert record["rejected_forbidden_count"] == 1
