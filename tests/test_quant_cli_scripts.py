import json
import subprocess
import sys
import os


def test_quant_fetch_portfolio_outputs_valid_json():
    """Run the CLI as a subprocess with stubbed FakeBroker and verify JSON on stdout."""
    env = {**os.environ, "QUANT_REVIEW_FAKE_BROKER": "1"}
    proc = subprocess.run(
        [sys.executable, "scripts/quant_fetch_portfolio.py"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "cash" in data
    assert "equity" in data
    assert "positions" in data
    assert isinstance(data["positions"], list)


def test_quant_fetch_externals_outputs_five_signals():
    """With QUANT_REVIEW_FAKE_EXTERNALS=1, verify the script emits 5 stubbed signals."""
    env = {**os.environ, "QUANT_REVIEW_FAKE_EXTERNALS": "1"}
    proc = subprocess.run(
        [sys.executable, "scripts/quant_fetch_externals.py"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert "signals" in data
    assert len(data["signals"]) == 5
    sources = {s["source"] for s in data["signals"]}
    assert sources == {"13F", "reddit", "etf-holdings", "ark", "congress"}


def test_quant_apply_reads_proposals_and_writes_outputs(tmp_path):
    proposals = {
        "date": "2026-04-19",
        "portfolio_summary": "ok",
        "macro_read": "risk-on",
        "reasoning_summary": "no big moves",
        "data_gaps": [],
        "proposed_changes": [
            {
                "key": "STOP_LOSS_PCT",
                "current_value": 0.08,
                "proposed_value": 0.075,
                "rationale": "ATR compressed",
                "detailed_plan": "tighter stop next rebalance",
                "expected_effect": "cuts losers faster",
                "risk_tier": "low",
                "confidence": 0.75,
            }
        ],
        "no_changes_reason": None,
    }
    proposals_path = tmp_path / "proposed.json"
    proposals_path.write_text(json.dumps(proposals))

    overrides_path = tmp_path / "overrides.json"
    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_path),
        "QUANT_APPLY_PROPOSALS_PATH": str(tmp_path / "proposals_out.json"),
        "QUANT_APPLY_TG_PATH": str(tmp_path / "tg.json"),
        "QUANT_APPLY_AUDIT_PATH": str(tmp_path / "audit.log"),
    }
    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", str(proposals_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    overrides = json.loads(overrides_path.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075


def test_quant_apply_dry_run_flag(tmp_path):
    proposals = {
        "date": "2026-04-19", "portfolio_summary": "", "macro_read": "",
        "reasoning_summary": "", "data_gaps": [],
        "proposed_changes": [
            {"key": "STOP_LOSS_PCT", "current_value": 0.08, "proposed_value": 0.075,
             "rationale": "r", "detailed_plan": "p", "expected_effect": "e",
             "risk_tier": "low", "confidence": 0.75}
        ],
        "no_changes_reason": None,
    }
    proposals_path = tmp_path / "proposed.json"
    proposals_path.write_text(json.dumps(proposals))
    overrides_path = tmp_path / "overrides.json"
    env = {
        **os.environ,
        "QUANT_APPLY_OVERRIDES_PATH": str(overrides_path),
        "QUANT_APPLY_PROPOSALS_PATH": str(tmp_path / "proposals_out.json"),
        "QUANT_APPLY_TG_PATH": str(tmp_path / "tg.json"),
        "QUANT_APPLY_AUDIT_PATH": str(tmp_path / "audit.log"),
        "QUANT_APPLY_DRY_PATH": str(tmp_path / "dry.json"),
    }
    proc = subprocess.run(
        [sys.executable, "scripts/quant_apply.py", "--dry-run", str(proposals_path)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert not overrides_path.exists()
    assert (tmp_path / "dry.json").exists()
