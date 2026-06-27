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
from concurrent.futures import ThreadPoolExecutor
from quant import paths
import shutil
import subprocess
from typing import Optional

import quant.agent.dossier as dossier

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


BUY_CANDIDATES_PATH = os.path.join(paths.REPO_ROOT, ".cache",
                                   "buy_candidates.json")
_SOURCE_MIX_PATH = os.path.join(paths.REPO_ROOT, ".cache", "agent_source_mix.csv")
_REASONS_LOG_PATH = os.path.join(paths.REPO_ROOT, ".cache", "agent_reasons.log")


def _default_fetchers():
    import quant.data.market as data
    import quant.signals.sentiment as sentiment
    import quant.config as config
    spy = None
    try:
        spy = data.fetch_ohlcv("SPY", period="1y")
    except Exception:
        spy = None
    news_fn = (lambda t: sentiment.fetch_yf_news([t])) if config.AGENT_INCLUDE_NEWS else (lambda t: None)
    return {"info_fn": data.fetch_info, "ohlcv_fn": lambda t: data.fetch_ohlcv(t, period="1y"),
            "est_fn": data.fetch_estimates, "news_fn": news_fn, "spy_ohlcv": spy}


def _log_monitoring(results, shortlist, picks):
    import datetime as _dt
    day = _dt.date.today().isoformat()
    pt = [p["ticker"] for p in picks]
    sc_pick, sc_short = source_counts(pt, results), source_counts(shortlist, results)
    try:
        os.makedirs(os.path.dirname(_SOURCE_MIX_PATH), exist_ok=True)
        new = not os.path.exists(_SOURCE_MIX_PATH)
        with open(_SOURCE_MIX_PATH, "a") as f:
            if new:
                f.write("date,n_picks,n_value,n_canslim,n_other,shortlist_value,shortlist_canslim\n")
            f.write(f"{day},{len(pt)},{sc_pick.get('value',0)},{sc_pick.get('canslim',0)},"
                    f"{sc_pick.get('other',0)},{sc_short.get('value',0)},{sc_short.get('canslim',0)}\n")
        with open(_REASONS_LOG_PATH, "a") as f:
            for p in picks:
                f.write(f"{day} {p['ticker']} {p.get('signal','?')} conf={p.get('confidence','?')} "
                        f"buy={p.get('buy_low')}-{p.get('buy_high')} stop={p.get('stop_loss')} "
                        f"tp={p.get('take_profit')} | {p.get('thesis','')}; risks: {p.get('risks','')}\n")
    except Exception as e:
        _log.warning("_log_monitoring: %s", e)

_GROUNDING = ("Use ONLY the numbers in each dossier; never invent a figure; "
              "null → 'unknown'. Reply with STRICT JSON only.")


def _to_int(x, default=0):
    """Tolerant int coercion — an LLM may emit confidence as "high"/"85%"/None.
    Never raises; falls back so a dirty field can't abort the pipeline."""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


def _analyst(dossiers, shortlist, llm_fn):
    rows = json.dumps([dossiers[t] for t in shortlist if t in dossiers], default=str)
    prompt = ("STAGE=ANALYST\nYou are a seasoned equity analyst. For EACH candidate, argue the "
              "bull case AND the bear case from the dossier, then give a verdict. " + _GROUNDING +
              ' Schema: {"verdicts":[{"ticker","signal":"bullish|neutral|bearish",'
              '"confidence":0-100,"thesis":"<=25w","risks","catalysts","bull","bear"}]}\n'
              "Reference: PE<20 cheap, rev_growth>15% strong, RSI>70 overbought, "
              "peer_relative z>+1 strong-vs-industry, target_upside>20% rich.\nDossiers:\n" + rows)
    data = _extract_json(llm_fn(prompt))
    out = {}
    if data and isinstance(data.get("verdicts"), list):
        for v in data["verdicts"]:
            t = v.get("ticker")
            if t in dossiers:
                out[t] = {"ticker": t, "signal": v.get("signal", "neutral"),
                          "confidence": _to_int(v.get("confidence")),
                          "thesis": str(v.get("thesis", ""))[:200], "risks": str(v.get("risks", ""))[:200],
                          "catalysts": str(v.get("catalysts", ""))[:200],
                          "bull": str(v.get("bull", ""))[:200], "bear": str(v.get("bear", ""))[:200]}
    # deterministic fallback for any shortlisted name the LLM didn't return
    for t in shortlist:
        if t in dossiers and t not in out:
            out[t] = {"ticker": t, "signal": "neutral", "confidence": 0, "thesis": "no analyst verdict",
                      "risks": "", "catalysts": "", "bull": "", "bear": ""}
    return out


def _critic(verdicts, dossiers, llm_fn):
    payload = json.dumps({"verdicts": list(verdicts.values()),
                          "dossiers": {t: dossiers[t] for t in verdicts if t in dossiers}}, default=str)
    prompt = ("STAGE=CRITIC\nYou are a skeptical risk reviewer. For each verdict, strike any claim "
              "not supported by the dossier numbers and CAP confidence that the data does not justify. "
              + _GROUNDING + ' Return the SAME schema plus "critic_notes". Input:\n' + payload)
    data = _extract_json(llm_fn(prompt))
    if not data or not isinstance(data.get("verdicts"), list):
        return verdicts                      # fallback: pass analyst verdicts through
    out = dict(verdicts)
    for v in data["verdicts"]:
        t = v.get("ticker")
        if t in out:
            out[t] = {**out[t], "confidence": _to_int(v.get("confidence"), out[t]["confidence"]),
                      "signal": v.get("signal", out[t]["signal"]),
                      "critic_notes": str(v.get("critic_notes", ""))[:200]}
    return out


def _pm(verdicts, llm_fn):
    import quant.config as config
    floor, cap = config.AGENT_CONVICTION_FLOOR, config.AGENT_MAX_PICKS
    eligible = {t: v for t, v in verdicts.items() if _to_int(v.get("confidence")) >= floor}
    prompt = ("STAGE=PM\nYou are the portfolio manager. From these analyst verdicts, choose the best "
              f"risk-adjusted set: buy ONLY names with confidence >= {floor}; return BETWEEN 0 and {cap} "
              "tickers; prefer cash to a weak buy. " + _GROUNDING +
              ' Schema: {"picks":[{"ticker","rationale":"<=15w"}]}\nVerdicts:\n'
              + json.dumps(list(verdicts.values()), default=str))
    data = _extract_json(llm_fn(prompt))
    picks = None
    if data and isinstance(data.get("picks"), list):
        chosen = [p.get("ticker") for p in data["picks"] if p.get("ticker") in eligible]
        picks = chosen[:cap]
    if picks is None:                        # fallback: floor-eligible by confidence
        picks = [t for t, _ in sorted(eligible.items(), key=lambda kv: -_to_int(kv[1].get("confidence")))][:cap]
    return picks


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


def _balanced_shortlist(results, pool, owned):
    """Deterministic, source-blind output: take each strategy's top-K (by its own
    rank), union + dedupe, cap at AGENT_SHORTLIST_N. Returns tickers only."""
    import quant.config as config
    seen, short = set(owned), []
    for name, payload in results.items():
        rows = sorted(payload.get("rows", []), key=lambda r: r.get("rank", 10**9))
        taken = 0
        for r in rows:
            t = r.get("ticker")
            if not t or t in seen:
                continue
            short.append(t); seen.add(t); taken += 1
            if taken >= config.AGENT_SHORTLIST_PER_SOURCE:
                break
    return short[:config.AGENT_SHORTLIST_N]


def _build_dossiers(pool, *, info_fn, ohlcv_fn, est_fn, news_fn, spy_ohlcv):
    import quant.config as config
    tickers = [e["ticker"] for e in pool]

    def _one(t):
        try:
            return t, dossier.build_dossier(
                t, info=info_fn(t), ohlcv=ohlcv_fn(t), spy_ohlcv=spy_ohlcv,
                news=news_fn(t), estimates=est_fn(t))
        except Exception as e:
            _log.warning("_build_dossiers: %s failed: %s", t, e)
            return t, None

    out = {}
    if tickers:
        with ThreadPoolExecutor(max_workers=config.AGENT_DOSSIER_WORKERS) as ex:
            for t, dos in ex.map(_one, tickers):
                if dos is not None:
                    out[t] = dos
    dossier.add_peer_relative(list(out.values()), min_group=config.AGENT_PEER_MIN_GROUP)
    return out


def source_counts(tickers, results):
    """Count how many of `tickers` came from each strategy (a ticker may count
    for multiple)."""
    by_src = {name: {r.get("ticker") for r in payload.get("rows", [])}
              for name, payload in results.items()}
    counts = {name: sum(1 for t in tickers if t in s) for name, s in by_src.items()}
    counts["other"] = sum(1 for t in tickers if not any(t in s for s in by_src.values()))
    return counts


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


def select_candidates(top_n=None, owned=None, llm_fn=None, *, fetchers=None) -> list:
    """Dossier-grounded pipeline: balanced blind shortlist → analyst → critic → PM
    (0..AGENT_MAX_PICKS, conviction floor) → enriched picks + advisory levels.
    Fail-open: any stage failure degrades to a deterministic fallback."""
    import quant.config as config
    import quant.strategies.contract as strategies
    llm_fn = llm_fn or _default_llm
    if owned is None:
        try:
            import quant.execution.orders as orders
            owned = {p["symbol"] for p in orders._load_portfolio_cache().get("positions", [])}
        except Exception:
            owned = set()

    results = strategies.load_strategy_results()
    pool = [e for e in _merge_pool(results) if e["ticker"] not in owned]
    by_ticker = {e["ticker"]: e for e in pool}

    picks = []
    shortlist = []
    if pool:
        # Function-level fail-open guard: any unexpected error still leaves picks=[]
        # and falls through to persist a (possibly empty) buy_candidates.json, so the
        # contract "never raises into the watchdog" holds at this function, not by luck
        # of the caller.
        try:
            fetchers = fetchers or _default_fetchers()
            dossiers = _build_dossiers(pool, **fetchers)
            shortlist = [t for t in _balanced_shortlist(results, pool, owned) if t in dossiers]
            if shortlist:
                verdicts = _critic(_analyst(dossiers, shortlist, llm_fn), dossiers, llm_fn)
                chosen = _pm(verdicts, llm_fn)
                for t in chosen:
                    v = verdicts.get(t, {})
                    lv = dossier.suggested_levels(dossiers[t], buy_band_atr=config.AGENT_BUY_BAND_ATR,
                                                  stop_atr_mult=config.AGENT_STOP_ATR_MULT,
                                                  target_r=config.AGENT_TARGET_R)
                    picks.append({"ticker": t, "rationale": v.get("thesis", ""), "signal": v.get("signal"),
                                  "confidence": v.get("confidence"), "thesis": v.get("thesis", ""),
                                  "risks": v.get("risks", ""), "catalysts": v.get("catalysts", ""),
                                  **lv, "strategies": by_ticker[t]["strategies"]})
        except Exception as e:
            _log.warning("select_candidates: pipeline failed, persisting empty picks: %s", e)
            picks = []

    _log_monitoring(results, shortlist, picks)
    os.makedirs(os.path.dirname(BUY_CANDIDATES_PATH), exist_ok=True)
    tmp = BUY_CANDIDATES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "picks": picks}, f)
    os.replace(tmp, BUY_CANDIDATES_PATH)
    return picks
