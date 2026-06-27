"""Value screen — one ensemble strategy. Two-track (profitable / unprofitable-
growth) selection over the Russell 3000, staged cheap→expensive. Thin
orchestrator: universe → prefilter → fundamentals → tracks → rank → write.
Fail-open; bounded by the strategy timeout when run from the daily watchdog."""
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor

import config
import discovery
import strategies
import value_tracks
from value_fundamentals import from_info
from value_prefilter import prefilter

_log = logging.getLogger(__name__)


def screen(universe, *, price_fn=None, info_fn=None):
    """Return ranked rows [{ticker, score, rank, factors}] (best first)."""
    from data import fetch_info
    info_fn = info_fn or fetch_info
    survivors = prefilter(universe, price_fn=price_fn)
    if not survivors:
        return []

    def _fund(t):
        try:
            return from_info(t, info_fn(t) or {})
        except Exception:
            return None

    workers = max(1, min(config.VS_FETCH_WORKERS, len(survivors)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        funds = [f for f in ex.map(_fund, survivors) if f is not None]

    picks = {"A": [], "B": []}
    for f in funds:
        tr = value_tracks.classify(f)
        if tr and value_tracks.passes(f, tr):
            picks[tr].append((f, value_tracks.score(f, tr)))
    picks["A"].sort(key=lambda x: -x[1])
    picks["B"].sort(key=lambda x: -x[1])

    rows, i = [], 0
    while (i < len(picks["A"]) or i < len(picks["B"])) and len(rows) < config.VS_TOP_N:
        for tr in ("A", "B"):
            if i < len(picks[tr]) and len(rows) < config.VS_TOP_N:
                f, sc = picks[tr][i]
                rows.append({"ticker": f.ticker, "score": round(float(sc), 4), "factors": {
                    "track": tr, "pe": f.pe, "peg": f.peg, "ps": f.ps,
                    "rev_growth": f.rev_growth, "gross_margin": f.gross_margin,
                    "market_cap": f.market_cap}})
        i += 1
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows


def run(tickers=None):
    universe = list(tickers) if tickers else discovery.get_russell3000_tickers()
    rows = screen(universe)
    strategies.write_strategy_result("value", rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Two-track Russell 3000 value screen")
    ap.add_argument("--tickers", default=None, help="comma-separated; default = Russell 3000")
    args = ap.parse_args()
    rows = run(args.tickers.split(",") if args.tickers else None)
    for r in rows:
        print(f"{r['rank']:>2}  {r['ticker']:<6} [{r['factors']['track']}]  score={r['score']:+.3f}")


if __name__ == "__main__":
    main()
