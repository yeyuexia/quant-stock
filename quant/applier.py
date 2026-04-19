"""Applier: classifies proposed changes, enforces bounds, writes output files.

Risk-tier classification is the authoritative source of truth — the agent's
own `risk_tier` field is a hint, not a decision. Applier always re-classifies
from scratch using the same rules config.py's override loader enforces.
"""
from __future__ import annotations
import logging
import os
from typing import Any, Optional

from quant.schema import ProposedChange, ApplierResult

LOG = logging.getLogger(__name__)


# ── Tier allowlists ──────────────────────────────────────────────

# Low-risk numeric keys: auto-applied if within relative band AND absolute bounds.
# key → (absolute_lo, absolute_hi, relative_pct_band)
_LOW_RISK_NUMERIC = {
    "STOP_LOSS_PCT":     (0.04, 0.20, 0.20),
    "TRAILING_STOP_PCT": (0.06, 0.25, 0.20),
    "CASH_BUFFER_PCT":   (0.02, 0.20, 0.50),
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
    "SCREEN_MIN_ROE",
    "SCREEN_MAX_PE",
    "SCREEN_MAX_DEBT_EQUITY",
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
        if not isinstance(proposed, (int, float)):
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
        if proposed_set < current_set:
            # Strict subset = removal detected → high-risk
            return "high"
        return "low"

    # 3. High-risk keys
    if key in _HIGH_RISK_KEYS:
        return "high"

    # 4. Everything else: forbidden
    return "forbidden"
