#!/usr/bin/env python3
"""Apply a QuantReview JSON (the agent's proposed_changes.json) via the
applier. Writes .cache/strategy_overrides.json, .cache/strategy_proposals.json,
.cache/telegram_notifications.json, and .cache/quant_review.log.

Usage:
    python3 scripts/quant_apply.py path/to/proposed_changes.json
    python3 scripts/quant_apply.py --dry-run path/to/proposed_changes.json

Env-var overrides for testing (not normally set):
    QUANT_APPLY_OVERRIDES_PATH
    QUANT_APPLY_PROPOSALS_PATH
    QUANT_APPLY_TG_PATH
    QUANT_APPLY_AUDIT_PATH
    QUANT_APPLY_DRY_PATH
"""
from __future__ import annotations
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposals_file", help="Path to the QuantReview JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write overrides/proposals; write dry artifact instead")
    args = ap.parse_args()

    import quant.applier as applier
    for env_name, attr_name in (
        ("QUANT_APPLY_OVERRIDES_PATH", "OVERRIDES_PATH"),
        ("QUANT_APPLY_PROPOSALS_PATH", "PROPOSALS_PATH"),
        ("QUANT_APPLY_TG_PATH", "TG_NOTIFY_PATH"),
        ("QUANT_APPLY_AUDIT_PATH", "AUDIT_LOG_PATH"),
        ("QUANT_APPLY_DRY_PATH", "DRY_RUN_PATH"),
    ):
        if env_name in os.environ:
            setattr(applier, attr_name, os.environ[env_name])

    with open(args.proposals_file) as f:
        review_data = json.load(f)

    from quant.schema import ProposedChange
    raw_changes = review_data.get("proposed_changes", [])
    changes = []
    malformed = []
    for rc in raw_changes:
        try:
            changes.append(ProposedChange(**rc))
        except (TypeError, KeyError) as e:
            malformed.append({"raw": rc, "error": str(e)})

    context = {
        "portfolio_summary": review_data.get("portfolio_summary", ""),
        "macro_read": review_data.get("macro_read", ""),
        "reasoning_summary": review_data.get("reasoning_summary", ""),
        "data_gaps": review_data.get("data_gaps", []),
        "no_changes_reason": review_data.get("no_changes_reason"),
    }

    result = applier.apply(changes, dry_run=args.dry_run, review_context=context)

    # Add any schema-level malformed entries from parse time
    result.rejected_malformed.extend(malformed)

    print(json.dumps({
        "applied_low": len(result.applied_low),
        "queued_high": len(result.queued_high),
        "rejected_forbidden": len(result.rejected_forbidden),
        "rejected_out_of_bounds": len(result.rejected_out_of_bounds),
        "rejected_malformed": len(result.rejected_malformed),
        "dry_run": args.dry_run,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
