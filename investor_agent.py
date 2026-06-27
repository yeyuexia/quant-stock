"""LLM-based investor agent review for screener results.

Shells out to the local `claude` CLI in non-interactive mode (`-p`). The
review is read-only commentary on the screener's top-N list — no file or
shell tool use needed, so we run with default permission mode (the previous
`bypassPermissions` was unnecessary and wider than needed).
"""
import datetime as dt
import json
import logging
import os
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


BUY_CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), ".cache",
                                   "buy_candidates.json")


def _merge_pool(results: dict) -> list:
    """Merge rows across strategies → deduped pool, best rank kept, strategies
    recorded. Consensus (appears in more strategies) sorts first, then best rank."""
    pool = {}
    for name, payload in results.items():
        for row in payload.get("rows", []):
            t = row.get("ticker")
            if not t:
                continue
            entry = pool.setdefault(t, {"ticker": t, "strategies": [],
                                        "best_rank": 10**9, "score": None})
            entry["strategies"].append(name)
            entry["best_rank"] = min(entry["best_rank"], int(row.get("rank", 10**9)))
            sc = row.get("score")
            if isinstance(sc, (int, float)):
                entry["score"] = sc if entry["score"] is None else max(entry["score"], sc)
    ranked = sorted(pool.values(),
                    key=lambda e: (-len(e["strategies"]), e["best_rank"]))
    return ranked


def _build_prompt(per_strategy: dict, pool: list, top_n: int) -> str:
    lines = ["You are a portfolio analyst. Pick the best BUY candidates.",
             f"Return STRICT JSON: {{\"picks\":[{{\"ticker\":..,\"rationale\":\"<=15 words\"}}]}} with EXACTLY {top_n} picks.",
             "Only choose tickers from this candidate pool:"]
    for e in pool:
        lines.append(f"  {e['ticker']} (strategies: {','.join(e['strategies'])}, best_rank {e['best_rank']})")
    lines.append("\nPer-strategy lists:")
    for name, payload in per_strategy.items():
        tickers = ", ".join(r["ticker"] for r in payload.get("rows", [])[:10])
        lines.append(f"  {name}: {tickers}")
    return "\n".join(lines)


def _default_llm(prompt: str):
    """Call the local claude CLI; None on any failure."""
    if shutil.which("claude") is None:
        _log.warning("select_candidates: `claude` CLI not on PATH")
        return None
    try:
        result = subprocess.run(["claude", "-p", prompt], capture_output=True,
                                text=True, timeout=_CLAUDE_TIMEOUT_SEC)
    except Exception as e:
        _log.warning("select_candidates: claude call failed: %s", e)
        return None
    if result.returncode != 0:
        _log.warning("select_candidates: claude exited %d", result.returncode)
        return None
    return result.stdout


def _parse_llm(text, valid_tickers, top_n) -> "list | None":
    """Parse {'picks':[{ticker,rationale}]}; None if unusable."""
    if not text:
        return None
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        picks = data["picks"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    out = []
    for p in picks:
        t = p.get("ticker")
        if t in valid_tickers:
            out.append({"ticker": t, "rationale": str(p.get("rationale", ""))[:120]})
    if len(out) < top_n:
        return None
    return out[:top_n]


def select_candidates(top_n=None, owned=None, llm_fn=None) -> list:
    """Review all strategy results, pick top_n buy candidates, persist them."""
    import config
    import strategies
    top_n = top_n if top_n is not None else config.ENSEMBLE_TOP_N
    llm_fn = llm_fn or _default_llm
    if owned is None:
        try:
            import orders
            owned = {p["symbol"] for p in orders._load_portfolio_cache().get("positions", [])}
        except Exception:
            owned = set()

    results = strategies.load_strategy_results()
    pool = [e for e in _merge_pool(results) if e["ticker"] not in owned]

    picks = None
    if pool:
        valid = {e["ticker"] for e in pool}
        by_ticker = {e["ticker"]: e for e in pool}
        parsed = _parse_llm(llm_fn(_build_prompt(results, pool, top_n)), valid, top_n)
        if parsed is not None:
            picks = [{"ticker": p["ticker"], "rationale": p["rationale"],
                      "strategies": by_ticker[p["ticker"]]["strategies"]}
                     for p in parsed]
        else:
            picks = [{"ticker": e["ticker"], "rationale": "rule-ranked fallback",
                      "strategies": e["strategies"]} for e in pool[:top_n]]
    picks = picks or []

    os.makedirs(os.path.dirname(BUY_CANDIDATES_PATH), exist_ok=True)
    tmp = BUY_CANDIDATES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "picks": picks}, f)
    os.replace(tmp, BUY_CANDIDATES_PATH)
    return picks
