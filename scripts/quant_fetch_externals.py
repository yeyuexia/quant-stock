#!/usr/bin/env python3
"""Run all five external-signal fetchers in parallel and emit combined JSON.

Used by the quant review subagent. Set QUANT_REVIEW_FAKE_EXTERNALS=1 to
return stubbed signals (no network). Otherwise, fetch live."""
from __future__ import annotations
import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _fake_signals():
    from quant.schema import ExternalSignal
    now = dt.datetime.now(dt.timezone.utc)
    return [
        ExternalSignal(source=s, as_of=now, data=[{"stub": True}])
        for s in ("13F", "reddit", "etf-holdings", "ark", "congress")
    ]


def main() -> int:
    if os.environ.get("QUANT_REVIEW_FAKE_EXTERNALS") == "1":
        signals = _fake_signals()
    else:
        from quant.data_sources import fetch_all_externals
        signals = fetch_all_externals()

    out = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signals": [
            {
                "source": s.source,
                "as_of": s.as_of.isoformat() if hasattr(s.as_of, "isoformat") else s.as_of,
                "data": s.data,
                "error": s.error,
            }
            for s in signals
        ],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
