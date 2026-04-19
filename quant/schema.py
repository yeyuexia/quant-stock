"""Shared dataclasses for the quant review subagent.

All types use plain dataclasses with JSON-serializable primitive fields so
they move cleanly between the agent (JSON files) and Python (applier).
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ExternalSignal:
    """One of the five external data feeds, normalized."""
    source: str                    # "13F" | "reddit" | "etf-holdings" | "ark" | "congress"
    as_of: dt.datetime             # freshness timestamp
    data: list                     # source-specific rows (list of dicts)
    error: Optional[str] = None    # set if fetch failed; data=[] in that case


@dataclass(frozen=True)
class ProposedChange:
    """A single parameter change the agent wants to make."""
    key: str                       # config key name (must be in allowlist at apply time)
    current_value: Any             # echo of current value (agent reads from prompt)
    proposed_value: Any
    rationale: str                 # paragraph: why — must cite specific data
    detailed_plan: str             # paragraph: what happens — concrete portfolio effect
    expected_effect: str           # short: e.g. "cuts losers 15% faster"
    risk_tier: str                 # "low" | "high" — agent pre-classifies; applier verifies
    confidence: float              # 0..1


@dataclass(frozen=True)
class QuantReview:
    """Top-level review object the agent produces."""
    date: str                      # ISO date
    portfolio_summary: str
    macro_read: str
    reasoning_summary: str
    data_gaps: list                # list[str]
    proposed_changes: list         # list[ProposedChange]
    no_changes_reason: Optional[str] = None


@dataclass
class ApplierResult:
    """Outcome of running the applier over a review's proposed changes."""
    applied_low: list = field(default_factory=list)
    queued_high: list = field(default_factory=list)
    rejected_forbidden: list = field(default_factory=list)
    rejected_out_of_bounds: list = field(default_factory=list)
    rejected_malformed: list = field(default_factory=list)
