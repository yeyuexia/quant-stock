#!/bin/bash
# Cron wrapper: cd to repo root, source .env, exec python3 with the given args.
# Usage from crontab:
#   */10 10-15 * * 1-5 /Users/zl/works/stock/scripts/cron-wrapper.sh executor.py >> /Users/zl/works/stock/.cache/executor.log 2>&1
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi
exec python3 "$@"
