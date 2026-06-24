"""Applier: classifies proposed changes, enforces bounds, writes output files.

Risk-tier classification is the authoritative source of truth — the agent's
own `risk_tier` field is a hint, not a decision. Applier always re-classifies
from scratch using the same rules config.py's override loader enforces.
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import sys
from typing import Any, Optional

from quant.schema import ProposedChange, ApplierResult

LOG = logging.getLogger(__name__)

# fileio lives at the repo root — add to sys.path so this module works when
# imported either as `quant.applier` or as a script from `scripts/quant_apply.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from fileio import (  # noqa: E402
    atomic_write_json, atomic_append_text, read_modify_write_json,
)


# ── Tier allowlists ──────────────────────────────────────────────

# Low-risk numeric keys: auto-applied if within relative band AND absolute bounds.
# key → (absolute_lo, absolute_hi, relative_pct_band)
_LOW_RISK_NUMERIC = {
    "STOP_LOSS_PCT":       (0.04, 0.20, 0.20),
    "ATR_STOP_MULTIPLIER": (1.0,  4.0,  0.25),
    "TRAILING_STOP_PCT":   (0.06, 0.25, 0.20),
    "CASH_BUFFER_PCT":     (0.02, 0.20, 0.50),
}

# Low-risk list keys: auto-applied on ADDITIONS only (removals bumped to high).
# key → max list size
_LOW_RISK_LISTS = {
    "WATCHLIST":            100,
    "NEWS_SHOCK_KEYWORDS":  30,
}

# High-risk keys: requires user approval.
_HIGH_RISK_KEYS = {
    "MOMENTUM_TOP_N",
    "ETF_ALLOCATION_PCT",
    "STOCK_ALLOCATION_PCT",
    "SCREEN_RS_MIN",
    "SCREEN_ADR_MIN",
    "SCREEN_EMA_FAST",
    "SCREEN_EMA_SLOW",
    "SCREEN_BASE_WEEKS_MIN",
    "SCREEN_BASE_WEEKS_MAX",
    "SCREEN_BASE_DEPTH_MAX",
    "MOMENTUM_LOOKBACK_MONTHS",
    "SAFE_HAVEN",
}

# Everything else is implicitly forbidden (default-deny).


def classify_change(change: ProposedChange) -> str:
    """Return one of: "low" | "high" | "forbidden" | "rejected_out_of_bounds"."""
    key = change.key
    proposed = change.proposed_value
    current = change.current_value

    # 1. Low-risk numeric keys
    if key in _LOW_RISK_NUMERIC:
        abs_lo, abs_hi, rel_band = _LOW_RISK_NUMERIC[key]
        # bool is technically a subclass of int — explicitly reject so an
        # agent that sends {"STOP_LOSS_PCT": True} doesn't silently
        # become STOP_LOSS_PCT=1.
        if isinstance(proposed, bool) or not isinstance(proposed, (int, float)):
            return "rejected_out_of_bounds"
        if not (abs_lo <= proposed <= abs_hi):
            return "rejected_out_of_bounds"
        if isinstance(current, (int, float)) and current > 0:
            rel = abs(proposed - current) / current
            if rel > rel_band:
                return "high"   # out of low-risk band → bumped up to high
        return "low"

    # 2. Low-risk list keys
    if key in _LOW_RISK_LISTS:
        max_size = _LOW_RISK_LISTS[key]
        if not isinstance(proposed, list) or not isinstance(current, list):
            return "rejected_out_of_bounds"
        if len(proposed) > max_size:
            return "rejected_out_of_bounds"
        current_set = set(current)
        proposed_set = set(proposed)
        if not proposed_set.issuperset(current_set):
            # Anything missing from proposed that was in current = removal → high-risk
            return "high"
        return "low"

    # 3. High-risk keys
    if key in _HIGH_RISK_KEYS:
        return "high"

    # 4. Everything else: forbidden
    return "forbidden"


# ── File paths (overridable for tests) ───────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache")
OVERRIDES_PATH = os.path.join(_CACHE_DIR, "strategy_overrides.json")
PROPOSALS_PATH = os.path.join(_CACHE_DIR, "strategy_proposals.json")
TG_NOTIFY_PATH = os.path.join(_CACHE_DIR, "telegram_notifications.json")
AUDIT_LOG_PATH = os.path.join(_CACHE_DIR, "quant_review.log")
DRY_RUN_PATH   = os.path.join(_CACHE_DIR, "quant_review_dry.json")


# ── Public API ───────────────────────────────────────────────────

def apply(
    changes: list,
    *,
    dry_run: bool = False,
    review_context: Optional[dict] = None,
) -> ApplierResult:
    """Classify + apply/queue/reject each proposed change.

    `review_context` is an optional dict with portfolio_summary, macro_read,
    reasoning_summary, data_gaps — used to enrich the TG report.

    In dry-run mode, nothing is written to strategy_overrides.json or
    strategy_proposals.json; instead a combined artifact goes to
    .cache/quant_review_dry.json. TG notification is still written so the
    user can see what would have happened."""
    result = ApplierResult()
    for change in changes:
        if not isinstance(change, ProposedChange):
            result.rejected_malformed.append({"raw": repr(change)})
            continue
        tier = classify_change(change)
        if tier == "low":
            result.applied_low.append(change)
        elif tier == "high":
            result.queued_high.append(change)
        elif tier == "forbidden":
            result.rejected_forbidden.append(change)
        else:
            result.rejected_out_of_bounds.append(change)

    if dry_run:
        _write_dry_run(changes, result)
    else:
        if result.applied_low:
            _merge_overrides(result.applied_low)
        if result.queued_high:
            _append_proposals(result.queued_high)

    _write_tg_notification(result, review_context)
    _append_audit_log(result, review_context, dry_run=dry_run)

    return result


# ── Helpers ──────────────────────────────────────────────────────

def _merge_overrides(low_changes: list) -> None:
    """Merge applied-low changes into strategy_overrides.json (lock-protected)."""
    def _mutate(existing):
        for c in low_changes:
            existing[c.key] = c.proposed_value
        return existing
    read_modify_write_json(OVERRIDES_PATH, _mutate, default={})


def _append_proposals(high_changes: list) -> None:
    """Append high-risk changes to strategy_proposals.json with expiry.

    Lock-protected so two quant runs can't generate colliding IDs or lose
    each other's proposals. Expiry is 24 hours from creation (was 21:35
    HKT local, which is wrong for any non-HKT operator).
    """
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(hours=24)
    today_slug = now.date().isoformat()

    def _mutate(existing):
        if not isinstance(existing, list):
            existing = []
        # Number new proposals continuing from existing count to avoid
        # collisions if the file already has same-day proposals.
        for idx, c in enumerate(high_changes, start=1 + len(existing)):
            existing.append({
                "id": f"prop_{today_slug}_{idx:02d}",
                "key": c.key,
                "current": c.current_value,
                "proposed": c.proposed_value,
                "rationale": c.rationale,
                "detailed_plan": c.detailed_plan,
                "expected_effect": c.expected_effect,
                "confidence": c.confidence,
                "created_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            })
        return existing
    read_modify_write_json(PROPOSALS_PATH, _mutate, default=[])


def _write_dry_run(changes: list, result: ApplierResult) -> None:
    data = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": True,
        "proposed": [_change_to_dict(c) for c in changes if isinstance(c, ProposedChange)],
        "classification": {
            "applied_low": [_change_to_dict(c) for c in result.applied_low],
            "queued_high": [_change_to_dict(c) for c in result.queued_high],
            "rejected_forbidden": [_change_to_dict(c) for c in result.rejected_forbidden],
            "rejected_out_of_bounds": [_change_to_dict(c) for c in result.rejected_out_of_bounds],
        },
    }
    atomic_write_json(DRY_RUN_PATH, data)


def _change_to_dict(c: ProposedChange) -> dict:
    return {
        "key": c.key,
        "current_value": c.current_value,
        "proposed_value": c.proposed_value,
        "rationale": c.rationale,
        "detailed_plan": c.detailed_plan,
        "expected_effect": c.expected_effect,
        "risk_tier": c.risk_tier,
        "confidence": c.confidence,
    }


def _write_tg_notification(result: ApplierResult,
                           review_context: Optional[dict]) -> None:
    """Append a formatted TG message to telegram_notifications.json."""
    import sys as _sys
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from notifications import append_notification
    message = _format_tg_message(result, review_context)
    # Target the quant subagent's own TG path (env-overridable + test-patchable);
    # falls back to config.TELEGRAM_NOTIFY_PATH inside the helper when None.
    append_notification(
        {"source": "quant-review", "message": message},
        path=TG_NOTIFY_PATH,
    )


def _format_tg_message(result: ApplierResult,
                       ctx: Optional[dict]) -> str:
    """Compose the multi-section daily review message."""
    lines = []
    today = dt.date.today().isoformat()
    lines.append(f"📊 Daily Strategy Review — {today}")
    lines.append("")
    if ctx:
        if "portfolio_summary" in ctx:
            lines.append(f"Portfolio: {ctx['portfolio_summary']}")
        if "macro_read" in ctx:
            lines.append(f"Macro read: {ctx['macro_read']}")
        if "data_gaps" in ctx:
            gaps = ctx["data_gaps"] or []
            lines.append(f"Data gaps: {', '.join(gaps) if gaps else 'none'}")
        if "reasoning_summary" in ctx:
            lines.append("")
            lines.append(f"Summary: {ctx['reasoning_summary']}")
        lines.append("")

    def _fmt_change(c: ProposedChange, idx: int, prop_id: Optional[str] = None) -> list:
        header = f"{idx}. {c.key}: {c.current_value} → {c.proposed_value}"
        if prop_id:
            header = f"[{prop_id}] " + header
        out = [header,
               f"   Why: {c.rationale}",
               f"   Plan: {c.detailed_plan}",
               f"   Effect: {c.expected_effect}",
               f"   Confidence: {c.confidence:.2f}"]
        if prop_id:
            out.append(f"   Approve: /strategy-approve {prop_id}")
            out.append(f"   Reject:  /strategy-reject  {prop_id}")
        return out

    if result.applied_low:
        lines.append("━" * 31)
        lines.append("✅ AUTO-APPLIED (low-risk)")
        for i, c in enumerate(result.applied_low, 1):
            lines.extend(_fmt_change(c, i))
            lines.append("")
    if result.queued_high:
        lines.append("━" * 31)
        lines.append("⏳ NEEDS YOUR APPROVAL (high-risk)")
        for i, c in enumerate(result.queued_high, 1):
            pid = f"prop_{today}_{i:02d}"
            lines.extend(_fmt_change(c, i, prop_id=pid))
            lines.append("")
    if result.rejected_forbidden or result.rejected_out_of_bounds or result.rejected_malformed:
        lines.append("━" * 31)
        lines.append("🚫 REJECTED")
        for c in result.rejected_forbidden:
            lines.append(f"   (forbidden) {c.key}: {c.current_value} → {c.proposed_value}")
        for c in result.rejected_out_of_bounds:
            lines.append(f"   (out of bounds) {c.key}: {c.current_value} → {c.proposed_value}")
        for m in result.rejected_malformed:
            lines.append(f"   (malformed) {m}")

    if not (result.applied_low or result.queued_high
            or result.rejected_forbidden or result.rejected_out_of_bounds):
        lines.append("No changes proposed today.")

    return "\n".join(lines)


def _append_audit_log(result: ApplierResult,
                      ctx: Optional[dict],
                      dry_run: bool) -> None:
    """Append-only JSON-lines audit. Lock-protected so concurrent writes
    don't interleave bytes."""
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": dry_run,
        "applied_low_count": len(result.applied_low),
        "queued_high_count": len(result.queued_high),
        "rejected_forbidden_count": len(result.rejected_forbidden),
        "rejected_out_of_bounds_count": len(result.rejected_out_of_bounds),
        "rejected_malformed_count": len(result.rejected_malformed),
        "context": ctx or {},
    }
    atomic_append_text(AUDIT_LOG_PATH, json.dumps(record, default=str))
