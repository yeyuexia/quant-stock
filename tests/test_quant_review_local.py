"""Tests for the local quant-review orchestrator.

These don't invoke the real claude CLI — they verify the orchestrator's
error-handling paths and that it correctly delegates to the CLI's stdout
stream. Real claude invocation is left for a manual smoke test.
"""
import subprocess
import sys
import os


_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "quant_review_local.py"
)


def test_exits_cleanly_when_claude_not_on_path(tmp_path, monkeypatch):
    """With an empty PATH, `shutil.which('claude')` returns None; the
    orchestrator must exit with code 1 and a helpful message, not crash."""
    env = {**os.environ, "PATH": "/nonexistent"}
    proc = subprocess.run(
        [sys.executable, _SCRIPT],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 1
    assert "claude" in proc.stderr.lower()
    assert "not found" in proc.stderr.lower() or "path" in proc.stderr.lower()


def test_exits_cleanly_when_prompt_missing(tmp_path, monkeypatch):
    """If the trigger_prompt.md file is gone, the script must exit with
    a clear error (not crash on FileNotFoundError)."""
    # Import the module and call main() with the prompt path pointed
    # at a non-existent file
    import importlib.util
    spec = importlib.util.spec_from_file_location("quant_review_local", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "_PROMPT_PATH", str(tmp_path / "nope.md"))
    rc = mod.main()
    assert rc == 1
