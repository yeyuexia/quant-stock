"""Value+Quality screen — one ensemble strategy. Ranks a universe by a
cross-sectional z-score of value + quality (+ optional improving) factors,
after hard liquidity/quality gates. Fail-open on missing data.
"""
import argparse
import logging
import math

import config
import strategies

_log = logging.getLogger(__name__)


def _price_of(info: dict) -> float:
    for k in ("currentPrice", "regularMarketPrice", "previousClose"):
        v = info.get(k)
        if isinstance(v, (int, float)) and v == v and v > 0:
            return float(v)
    return 0.0


def _finite(v):
    return isinstance(v, (int, float)) and v == v and abs(v) != float("inf")


def _passes_gates(info: dict) -> bool:
    price = _price_of(info)
    if price < config.VS_MIN_PRICE:
        return False
    advol = info.get("averageVolume")
    if not _finite(advol) or price * float(advol) < config.VS_MIN_DOLLAR_VOLUME:
        return False
    mcap = info.get("marketCap")
    if not _finite(mcap) or float(mcap) < config.VS_MIN_MARKET_CAP:
        return False
    fcf = info.get("freeCashflow")
    roe = info.get("returnOnEquity")
    fcf_pos = _finite(fcf) and fcf > 0
    roe_pos = _finite(roe) and roe > 0
    if not (fcf_pos or roe_pos):   # trap guard
        return False
    return True


def _raw_factors(info: dict, fund: dict) -> dict:
    """Each factor: higher = more attractive. Absent inputs → key omitted."""
    f = {}
    mcap = info.get("marketCap")
    fcf = info.get("freeCashflow")
    if _finite(fcf) and _finite(mcap) and mcap > 0:
        f["fcf_yield"] = float(fcf) / float(mcap)
    fpe = info.get("forwardPE")
    if _finite(fpe) and fpe > 0:
        f["earnings_yield"] = 1.0 / float(fpe)
    ev = info.get("enterpriseToEbitda")
    if _finite(ev) and ev > 0:
        f["ev_ebitda_inv"] = 1.0 / float(ev)
    p2b = info.get("priceToBook")
    if _finite(p2b) and p2b > 0:
        f["book_market"] = 1.0 / float(p2b)
    roe = info.get("returnOnEquity")
    if _finite(roe):
        f["roe"] = float(roe)
    d2e = info.get("debtToEquity")
    if _finite(d2e) and d2e >= 0:
        f["inv_debt"] = 1.0 / (1.0 + float(d2e))
    if _finite(fund.get("eps_q_growth")):
        f["eps_q_growth"] = float(fund["eps_q_growth"])
    if _finite(fund.get("revenue_growth")):
        f["revenue_growth"] = float(fund["revenue_growth"])
    return f


_GROUPS = {
    "value": ("fcf_yield", "earnings_yield", "ev_ebitda_inv", "book_market"),
    "quality": ("roe", "inv_debt"),
    "improving": ("eps_q_growth", "revenue_growth"),
}


def _zscores(values: list) -> list:
    """Winsorized (±3σ) z-scores; all-equal/empty → zeros."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return [0.0 for _ in values]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var)
    if sd == 0:
        return [0.0 for _ in values]
    out = []
    for v in values:
        if v is None:
            out.append(0.0)
        else:
            z = (v - mean) / sd
            out.append(max(-3.0, min(3.0, z)))
    return out


def screen_value_quality(tickers, *, info_fn=None, fund_fn=None, price_fn=None):
    """Return ranked rows [{ticker, score, rank, factors}] (best first)."""
    from data import fetch_info, fetch_fundamentals
    info_fn = info_fn or fetch_info
    fund_fn = fund_fn or fetch_fundamentals

    survivors = []
    for t in tickers:
        try:
            info = info_fn(t) or {}
        except Exception as e:
            _log.warning("value_screen: fetch_info(%s) failed: %s", t, e)
            continue
        if not info or not _passes_gates(info):
            continue
        try:
            fund = fund_fn(t) or {}
        except Exception:
            fund = {}
        survivors.append({"ticker": t, "raw": _raw_factors(info, fund),
                          "price": _price_of(info)})

    if not survivors:
        return []

    # Cross-sectional z per raw factor.
    all_keys = set().union(*[s["raw"].keys() for s in survivors])
    zmap = {}
    for key in all_keys:
        col = [s["raw"].get(key) for s in survivors]
        for s, z in zip(survivors, _zscores(col)):
            zmap.setdefault(id(s), {})[key] = z

    rows = []
    for s in survivors:
        zs = zmap[id(s)]
        group_z = {}
        for g, keys in _GROUPS.items():
            present = [zs[k] for k in keys if k in zs]
            group_z[g] = sum(present) / len(present) if present else 0.0
        score = sum(config.VS_WEIGHTS[g] * group_z[g] for g in config.VS_WEIGHTS)
        rows.append({
            "ticker": s["ticker"], "score": round(score, 4),
            "factors": {**{k: round(v, 4) for k, v in s["raw"].items()},
                        "value_z": round(group_z["value"], 4),
                        "quality_z": round(group_z["quality"], 4),
                        "improving_z": round(group_z["improving"], 4),
                        "price": s["price"]},
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    rows = rows[:config.VS_TOP_N]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def run(tickers=None):
    rows = screen_value_quality(list(tickers or config.WATCHLIST))
    strategies.write_strategy_result("value", rows)
    return rows


def main():
    ap = argparse.ArgumentParser(description="Value+Quality screen")
    ap.add_argument("--tickers", default=None,
                    help="comma-separated; defaults to config.WATCHLIST")
    args = ap.parse_args()
    tickers = args.tickers.split(",") if args.tickers else None
    rows = run(tickers)
    for r in rows:
        print(f"{r['rank']:>2}  {r['ticker']:<6}  score={r['score']:+.3f}  "
              f"V={r['factors']['value_z']:+.2f} Q={r['factors']['quality_z']:+.2f}")


if __name__ == "__main__":
    main()
