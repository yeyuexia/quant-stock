"""LLM-based investor agent review for screener results.

Shells out to the local `claude` CLI in non-interactive mode (`-p`). The
review is read-only commentary on the screener's top-N list — no file or
shell tool use needed, so we run with default permission mode (the previous
`bypassPermissions` was unnecessary and wider than needed).
"""
import logging
import shutil
import subprocess
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

_CLAUDE_TIMEOUT_SEC = 120
_MAX_ROWS_IN_PROMPT = 20      # cap to avoid runaway token cost on huge screens


def _format_df(df: pd.DataFrame) -> str:
    lines = []
    for _, row in df.iterrows():
        vcp = f"VCP({int(row['vcp_contractions'])})" if row.get("in_base") else "no-vcp"
        vol = "vol✓" if row.get("vol_contracting") else ""
        pivot = f"Pivot:${row['vcp_pivot']:.2f}" if row.get("vcp_pivot") else ""
        eps_g = f"EPS:{row['eps_q_growth']*100:+.0f}%" if row.get("eps_q_growth") is not None else ""
        rev_g = f"Rev:{row['rev_growth']*100:+.0f}%" if row.get("rev_growth") is not None else ""
        accel = "↑" if row.get("eps_accel") else ""
        parts = [p for p in [vcp, vol, pivot, eps_g, rev_g + accel] if p]
        lines.append(
            f"#{int(row['rank'])} {row['ticker']} ${row['price']:.2f} RS:{row['rs_score']:.0f}"
            f" ADR:{row['adr']*100:.1f}% | {' '.join(parts)}"
        )
    return "\n".join(lines)


def run_investor_review(df: pd.DataFrame) -> Optional[str]:
    """Ask the local claude CLI for a short review of the screened stocks.

    Returns commentary text, or None on any failure (missing CLI, timeout,
    nonzero exit, empty stdout). Failures are logged at WARNING — they used
    to be completely silent which made it impossible to tell whether the
    LLM was skipped vs returned blank.
    """
    if df.empty:
        return None

    # Cap the prompt size so a giant screener result doesn't burn tokens.
    df = df.head(_MAX_ROWS_IN_PROMPT)
    summary = _format_df(df)
    prompt = (
        "你是一位资深 CANSLIM 投资人。以下是今日筛选出的技术领先股：\n\n"
        f"{summary}\n\n"
        "请简要评价（3-5句话）：\n"
        "1. 哪只股票最值得关注？理由是什么？\n"
        "2. 有没有需要注意的风险或不确定性？\n"
        "3. 整体市场背景下的建议。\n"
        "回答简洁，直接给出判断，不要重复股票数据。"
    )

    # Fail fast with a clear log line if the claude CLI isn't installed —
    # previous behavior was a silent FileNotFoundError catch that made
    # "review never happens" indistinguishable from "review came back empty".
    if shutil.which("claude") is None:
        _log.warning("run_investor_review: `claude` CLI not on PATH; skipping")
        return None

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        _log.warning("run_investor_review: claude CLI timed out after %ds",
                     _CLAUDE_TIMEOUT_SEC)
        return None
    except Exception as e:
        _log.warning("run_investor_review: subprocess failed: %s", e)
        return None

    if result.returncode != 0:
        _log.warning(
            "run_investor_review: claude CLI exited %d; stderr=%r",
            result.returncode, (result.stderr or "")[:200],
        )
        return None

    text = (result.stdout or "").strip()
    return text if text else None
