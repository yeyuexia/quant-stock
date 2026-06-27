#!/usr/bin/env python3
"""Dump current portfolio state as JSON to stdout.

Used by the quant review subagent via Bash. The agent parses stdout directly.
Set QUANT_REVIEW_FAKE_BROKER=1 in the environment to use the in-memory
FakeBroker (for tests / smoke runs without Alpaca credentials)."""
from __future__ import annotations
import json
import os
import sys

# Add project root to sys.path so we can import project modules regardless of cwd
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import quant.config as config


def main() -> int:
    if os.environ.get("QUANT_REVIEW_FAKE_BROKER") == "1":
        from tests.fakes import FakeBroker
        broker = FakeBroker(cash=100_000.0, equity=100_000.0)
        # sync_state persists to orders.PORTFOLIO_PATH; in fake mode redirect
        # to a throwaway tmp dir so test runs / smokes don't clobber the
        # real portfolio.json on the host. Register cleanup so we don't
        # leave a growing pile of `quant_fetch_fake_*` dirs in /tmp.
        import atexit
        import shutil
        import tempfile
        import quant.execution.orders as _orders
        _fake_dir = tempfile.mkdtemp(prefix="quant_fetch_fake_")
        _orders.PORTFOLIO_PATH = os.path.join(_fake_dir, "portfolio.json")
        _orders.DAILY_LOG_PATH = os.path.join(_fake_dir, "daily_log.csv")
        atexit.register(shutil.rmtree, _fake_dir, ignore_errors=True)
    else:
        from quant.execution.broker import Broker
        broker = Broker(env=config.ALPACA_ENV)

    from quant.execution.orders import sync_state
    snap = sync_state(broker, alerts=[])

    out = {
        "as_of": snap.synced_at,
        "alpaca_env": snap.alpaca_env,
        "cash": snap.cash,
        "equity": snap.equity,
        "positions": snap.positions,
        "tranches": snap.tranches,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
