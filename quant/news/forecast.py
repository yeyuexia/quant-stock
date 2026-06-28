#!/usr/bin/env python3
"""
Political forecast.

Uses ANTHROPIC_API_KEY (SDK) when available — no subscription daily limit.
Falls back to claude CLI if ANTHROPIC_API_KEY is not set.

Two analysis modes:
  hotspot   — immediate, fast model
  scheduled — full briefing, sonnet

Usage:
  python3 forecast.py              # print latest briefing
  python3 forecast.py --hotspots   # show recent hotspot alerts
"""
import os
from quant import paths
import sys
import json
import logging
import subprocess
import datetime as dt
from typing import Dict, List, Optional

# Load .env so ANTHROPIC_API_KEY is available when run standalone
_env_path = os.path.join(paths.REPO_ROOT, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from quant.news.news_store import (
    init_db, get_articles_for_briefing, insert_analysis,
    get_latest_analysis, count_hotspot_llm_calls,
)

logger = logging.getLogger(__name__)

_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}


def _call_claude(prompt: str, system: str, model: str = "sonnet", timeout: int = 120) -> str:
    """Call Claude API.

    Uses Anthropic SDK (ANTHROPIC_API_KEY) if available — bypasses the Claude
    Code subscription daily limit. Falls back to the claude CLI otherwise.
    Add ANTHROPIC_API_KEY to .env to fix daily-limit failures at 5pm ET.
    """
    model_id = _MODEL_ALIASES.get(model, model)

    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic as _anthropic
        client = _anthropic.Anthropic(timeout=timeout)
        response = client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        raise RuntimeError("No text content in Claude response")

    # Fallback: claude CLI (uses subscription, subject to daily limits)
    logger.warning("ANTHROPIC_API_KEY not set — using claude CLI (hits daily limit)")
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    cmd = [
        claude_bin, "--print",
        "--output-format", "text",
        "--model", model,
        "--append-system-prompt", system,
        "-p", prompt,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse_json_response(text: str, fallback: Dict) -> Dict:
    """Extract JSON from claude response, stripping markdown fences and preamble."""
    text = text.strip()

    # Try raw parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        candidate = "\n".join(inner).strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            text = candidate

    # Find the outermost JSON object by matching braces
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break

    logger.warning("Failed to parse JSON from claude response, using fallback")
    return fallback


def analyze_hotspot(category: str, articles: List[Dict]) -> Dict:
    """Call Claude for immediate hotspot analysis. Stores result in DB."""
    article_text = "\n".join(f"- {a['title']}" for a in articles[:10])

    system = (
        "You are a financial analyst. "
        "Respond only in valid JSON with no markdown fences."
    )
    prompt = (
        f"Breaking news cluster: {category}\n\n"
        f"Articles:\n{article_text}\n\n"
        "Respond in JSON:\n"
        '{"summary": "2 sentence market impact summary", '
        '"sector_impacts": {"TICKER": "bullish|bearish|neutral"}, '
        '"confidence": "low|medium|high", '
        '"political_risk_score": 0.0}'
    )

    fallback = {
        "summary": "Analysis unavailable.",
        "sector_impacts": {},
        "confidence": "low",
        "political_risk_score": 0.0,
    }

    try:
        raw = _call_claude(prompt, system, model="haiku", timeout=180)
        result = _parse_json_response(raw, fallback)
        result["political_risk_score"] = float(result.get("political_risk_score", 0.0))
    except Exception as e:
        logger.warning("Hotspot LLM call failed: %s", e)
        result = fallback

    insert_analysis(
        trigger="hotspot",
        category=category,
        input_summary=article_text[:500],
        briefing=result.get("summary", ""),
        sector_impacts=result.get("sector_impacts", {}),
        political_risk_score=result["political_risk_score"],
    )
    return result


def run_scheduled_briefing(hours: int = 8) -> Dict:
    """Call Claude Sonnet for full scheduled briefing. Stores result in DB."""
    init_db()
    articles = get_articles_for_briefing(hours=hours)

    if not articles:
        fallback = {
            "briefing": "No recent political or macro events detected.",
            "sector_impacts": {},
            "political_risk_score": 0.0,
            "key_risks": [],
        }
        insert_analysis(
            trigger="scheduled", category=None,
            input_summary="(no articles)", briefing=fallback["briefing"],
            sector_impacts={}, political_risk_score=0.0,
        )
        return fallback

    # Summarize by category to keep prompt concise
    by_category: Dict[str, List[str]] = {}
    for a in articles:
        cat = a.get("category", "other")
        by_category.setdefault(cat, []).append(a["title"])

    event_summary = "\n".join(
        f"{cat.upper()}: " + " | ".join(titles[:5])
        for cat, titles in by_category.items()
    )

    try:
        from macro import macro_regime_score
        macro = macro_regime_score()
        macro_str = f"{macro['regime'].upper()} (score: {macro['score']:+.2f})"
    except Exception:
        macro_str = "N/A"

    last = get_latest_analysis()
    prev_score = last["political_risk_score"] if last else 0.0

    system = (
        "You are a senior portfolio analyst covering US equities. "
        "Analyze political and macro news and their likely impact on US stocks. "
        "Respond only in valid JSON with no markdown fences."
    )
    prompt = (
        f"Recent events (last {hours}h):\n{event_summary}\n\n"
        f"Current macro regime: {macro_str}\n"
        f"Previous political_risk_score: {prev_score:+.2f}\n\n"
        "Respond in JSON:\n"
        '{"briefing": "2-3 paragraph summary of key developments and market implications", '
        '"sector_impacts": {"TICKER": "bullish|bearish|neutral"}, '
        '"political_risk_score": 0.0, '
        '"key_risks": ["risk1", "risk2"]}'
    )

    fallback = {
        "briefing": "Analysis unavailable.",
        "sector_impacts": {},
        "political_risk_score": 0.0,
        "key_risks": [],
    }

    try:
        raw = _call_claude(prompt, system, model="sonnet", timeout=180)
        result = _parse_json_response(raw, fallback)
        result["political_risk_score"] = float(result.get("political_risk_score", 0.0))
    except Exception as e:
        logger.warning("Scheduled briefing LLM call failed: %s", e)
        result = fallback

    insert_analysis(
        trigger="scheduled", category=None,
        input_summary=event_summary[:500],
        briefing=result.get("briefing", ""),
        sector_impacts=result.get("sector_impacts", {}),
        political_risk_score=result["political_risk_score"],
    )
    return result


def get_latest_political_score() -> float:
    """Return most recent political_risk_score. Defaults to 0.0 (neutral)."""
    init_db()
    latest = get_latest_analysis()
    return latest["political_risk_score"] if latest else 0.0


def _print_latest_briefing():
    init_db()
    latest = get_latest_analysis()
    if not latest:
        print("  No briefing available yet. Run: python3 news_poller.py")
        return

    print(f"\n{'='*60}")
    print("  POLITICAL BRIEFING")
    print(f"{'='*60}")
    print(f"  Trigger:  {latest['trigger'].upper()}")
    print(f"  Time:     {latest['created_at']} UTC")
    print(f"  Pol.Risk: {latest['political_risk_score']:+.2f}")
    print()
    print(latest["briefing"])
    print()

    impacts = latest.get("sector_impacts", {})
    if impacts:
        print("  Sector Impacts:")
        for ticker, direction in impacts.items():
            icon = "↑" if direction == "bullish" else "↓" if direction == "bearish" else "→"
            print(f"    {ticker:6s} {direction} {icon}")
    print()


def _print_hotspots():
    init_db()
    from news_store import _get_conn
    cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=24)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM llm_analyses WHERE trigger='hotspot' AND created_at > ? "
            "ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()

    if not rows:
        print("  No hotspot alerts in the last 24 hours.")
        return

    print("\n  ── Hotspot Alerts (last 24h) ──")
    for row in rows:
        d = dict(row)
        print(f"  [{d['created_at']}] {d['category'].upper()} | risk: {d['political_risk_score']:+.2f}")
        print(f"    {d['briefing'][:100]}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--hotspots" in args:
        _print_hotspots()
    else:
        _print_latest_briefing()
