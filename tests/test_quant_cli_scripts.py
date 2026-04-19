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
