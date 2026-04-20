#!/usr/bin/env python3
"""Local quant review orchestrator.

Invokes the `claude` CLI (user's Claude Code subscription) to run the daily
strategy review workflow defined in quant/trigger_prompt.md. Runs on the
user's local machine via cron; sources .env for Alpaca credentials; the
agent's writes to .cache/ take effect on the local filesystem so config.py
picks up the overrides on the next rebalancer run.

Invoked by cron:
    0 7 * * 2-6 /Users/zl/works/stock/scripts/cron-wrapper.sh \
        scripts/quant_review_local.py \
        >> /Users/zl/works/stock/.cache/quant.log 2>&1

Requires:
- `claude` CLI installed and logged into the user's Claude Code subscription
- ALPACA_API_KEY / ALPACA_API_SECRET in the environment (cron-wrapper.sh
  sources .env automatically)
"""
from __future__ import annotations
import datetime as dt
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_PROMPT_PATH = os.path.join(_REPO, "quant", "trigger_prompt.md")
_CACHE = os.path.join(_REPO, ".cache")

# 15 min is generous; live smoke showed ~60s wall-clock for data fetch +
# whatever the LLM takes to reason (typically <2 min on Opus).
_TIMEOUT_SECONDS = 900


def _log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] {msg}")


def main() -> int:
    if not os.path.exists(_PROMPT_PATH):
        print(f"ERROR: trigger prompt not found at {_PROMPT_PATH}", file=sys.stderr)
        return 1

    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("ERROR: `claude` CLI not found on PATH. Install Claude Code, "
              "or ensure the cron-wrapper's PATH includes its install dir.",
              file=sys.stderr)
        return 1

    with open(_PROMPT_PATH) as f:
        prompt = f.read()

    os.makedirs(_CACHE, exist_ok=True)

    # --permission-mode bypassPermissions is required for headless runs —
    # otherwise the agent halts on the first Bash call waiting for approval
    # that can't be granted in non-interactive mode. Safe in our context:
    # the agent only runs our own scripts/*.py helpers and reads/writes
    # inside .cache/, bounded by the trigger prompt's workflow.
    _log(f"Invoking `{claude_bin}` for quant review (timeout {_TIMEOUT_SECONDS}s)")
    try:
        result = subprocess.run(
            [
                claude_bin,
                "--permission-mode", "bypassPermissions",
                "-p", prompt,
            ],
            capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, cwd=_REPO,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: claude timed out after {_TIMEOUT_SECONDS}s",
              file=sys.stderr)
        return 2

    if result.returncode != 0:
        print(f"ERROR: claude exited with code {result.returncode}",
              file=sys.stderr)
        if result.stderr:
            print("--- stderr (last 2000 chars) ---", file=sys.stderr)
            print(result.stderr[-2000:], file=sys.stderr)
        return 3

    # The agent's workflow should have written these (via Bash-invoking
    # scripts/quant_apply.py). Verify and log.
    expected = {
        "proposed_changes": os.path.join(_CACHE, "proposed_changes.json"),
        "tg_notification":  os.path.join(_CACHE, "telegram_notifications.json"),
        "audit_log":        os.path.join(_CACHE, "quant_review.log"),
    }
    for label, path in expected.items():
        status = "written" if os.path.exists(path) else "MISSING"
        _log(f"  {label}: {status}  ({path})")

    _log("Quant review complete.")
    if result.stdout:
        # Agent's final-response summary — useful audit trail in the log.
        print("--- agent final response (last 2000 chars) ---")
        print(result.stdout[-2000:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
