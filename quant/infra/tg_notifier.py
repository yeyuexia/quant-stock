"""
Telegram push notification helper.

Sends messages to the configured TELEGRAM_USER_ID via Bot HTTP API.
Uses only stdlib urllib — no async, no extra dependencies.
"""
import os
from quant import paths
import json
import logging
import urllib.request
from typing import Dict

logger = logging.getLogger(__name__)

# Load .env
_env_path = os.path.join(paths.REPO_ROOT, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
MAX_LEN = 4000


def _send_raw(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_USER_ID:
        logger.warning("Telegram tokens not set — message not sent")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_USER_ID, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def send_message(text: str):
    """Send message, splitting at newlines if >4000 chars."""
    if len(text) <= MAX_LEN:
        _send_raw(text)
        return
    lines = text.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > MAX_LEN:
            _send_raw(chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        _send_raw(chunk)


def send_hotspot_alert(category: str, analysis: Dict):
    sectors = analysis.get("sector_impacts", {})
    sector_str = "  ".join(
        f"{t}{'↑' if d == 'bullish' else '↓' if d == 'bearish' else '→'}"
        for t, d in list(sectors.items())[:6]
    )
    text = (
        f"HOTSPOT: {category.upper()}\n"
        f"Confidence: {analysis.get('confidence', '?')}\n\n"
        f"{analysis.get('summary', '')}\n\n"
        f"Sectors: {sector_str}"
    )
    send_message(text)


def send_scheduled_briefing(analysis: Dict, label: str = "BRIEFING"):
    sectors = analysis.get("sector_impacts", {})
    sector_lines = "\n".join(
        f"  {t:6s} {'bullish ↑' if d == 'bullish' else 'bearish ↓' if d == 'bearish' else 'neutral →'}"
        for t, d in list(sectors.items())[:8]
    )
    risks = ", ".join(analysis.get("key_risks", [])[:3]) or "none"
    score = analysis.get("political_risk_score", 0.0)
    text = (
        f"{label}\n\n"
        f"{analysis.get('briefing', '')}\n\n"
        f"Sector Impacts:\n{sector_lines}\n\n"
        f"Political Risk Score: {score:+.2f}\n"
        f"Key Risks: {risks}"
    )
    send_message(text)
