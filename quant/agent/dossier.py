"""Pure per-candidate dossier assembly for the investor agent. No network I/O —
all inputs (info dict, OHLCV frames, news, estimates) are passed in, so every
function here is deterministic and unit-testable."""
from typing import Optional

import pandas as pd

from quant.data.fundamentals import from_info
from quant.signals.indicators import atr


def _pct_from(price: Optional[float], ref: Optional[float]) -> Optional[float]:
    if price is None or ref is None or ref == 0:
        return None
    return price / ref - 1.0


def _rsi(close: "pd.Series", period: int) -> Optional[float]:
    if close is None or len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
    if loss == 0:
        return 100.0
    rs = gain / loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _rel_strength(tkr_close, spy_close, lookback: int) -> Optional[float]:
    if tkr_close is None or spy_close is None:
        return None
    if len(tkr_close) <= lookback or len(spy_close) <= lookback:
        return None
    t = tkr_close.iloc[-1] / tkr_close.iloc[-lookback - 1] - 1.0
    s = spy_close.iloc[-1] / spy_close.iloc[-lookback - 1] - 1.0
    return float(t - s)


def _zscore(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return [None] * len(values)
    mean = sum(nums) / len(nums)
    var = sum((v - mean) ** 2 for v in nums) / len(nums)
    sd = var ** 0.5
    if sd == 0:
        return [0.0 if isinstance(v, (int, float)) else None for v in values]
    return [((v - mean) / sd) if isinstance(v, (int, float)) else None for v in values]


def _series(ohlcv, field, ticker):
    """Extract a field Series for `ticker` from a MultiIndex (field, ticker) frame."""
    if ohlcv is None or len(ohlcv) == 0:
        return None
    try:
        sub = ohlcv[field]
    except Exception:
        return None
    if ticker in getattr(sub, "columns", []):
        return sub[ticker].dropna()
    # single-ticker frame may have a lone column
    if hasattr(sub, "columns") and len(sub.columns) == 1:
        return sub.iloc[:, 0].dropna()
    return None


def _last(series):
    return float(series.iloc[-1]) if series is not None and len(series) else None


def build_dossier(ticker, *, info, ohlcv=None, spy_ohlcv=None, news=None, estimates=None) -> dict:
    import quant.config as config
    f = from_info(ticker, info or {})
    close = _series(ohlcv, "Close", ticker)
    high = _series(ohlcv, "High", ticker)
    low = _series(ohlcv, "Low", ticker)
    spy_close = _series(spy_ohlcv, "Close", "SPY") if spy_ohlcv is not None else None

    price = (info or {}).get("currentPrice") or _last(close)
    hi52 = (info or {}).get("fiftyTwoWeekHigh")
    lo52 = (info or {}).get("fiftyTwoWeekLow")
    dma50 = float(close.rolling(50).mean().iloc[-1]) if close is not None and len(close) >= 50 else None
    dma200 = float(close.rolling(200).mean().iloc[-1]) if close is not None and len(close) >= 200 else None
    atr14 = None
    if high is not None and low is not None and close is not None and len(close) > config.AGENT_RSI_PERIOD:
        try:
            _atr_result = atr(high, low, close, config.AGENT_RSI_PERIOD)
            if _atr_result is not None:
                atr14 = float(_atr_result) if not hasattr(_atr_result, "iloc") else float(_atr_result.iloc[-1])
        except Exception:
            atr14 = None
    swing_low_20 = float(low.tail(20).min()) if low is not None and len(low) >= 20 else None
    swing_high_20 = float(high.tail(20).max()) if high is not None and len(high) >= 20 else None

    tgt = (info or {}).get("targetMeanPrice")
    est = estimates or {}
    news_section = None
    if news is not None:
        from quant.signals.sentiment import analyze_news_sentiment
        sent = analyze_news_sentiment(news) if news else {}
        news_section = {"count": len(news),
                        "sentiment_score": sent.get("score"),
                        "sentiment_label": sent.get("label"),
                        "headlines": [n.get("title", "") for n in news[:3]]}

    return {
        "ticker": ticker,
        "sector": (info or {}).get("sector"),
        "valuation": {"pe": f.pe, "peg": f.peg, "ev_ebitda": f.ev_ebitda, "ps": f.ps,
                      "fcf_yield": (f.fcf / f.market_cap) if (f.fcf is not None and f.market_cap) else None},
        "quality": {"gross_margin": f.gross_margin, "op_margin": f.op_margin,
                    "roe": (info or {}).get("returnOnEquity"), "debt_equity": f.debt_equity,
                    "current_ratio": f.current_ratio, "profitable": f.is_profitable},
        "growth": {"rev_growth": f.rev_growth, "eps_growth": f.eps_growth},
        "estimates": {"revision_trend": est.get("revision_trend"),
                      "up_revisions_90d": est.get("up_revisions_90d"),
                      "down_revisions_90d": est.get("down_revisions_90d"),
                      "surprises": est.get("surprises", [])},
        "price_action": {"price": price,
                         "pct_from_52w_high": _pct_from(price, hi52),
                         "pct_from_52w_low": _pct_from(price, lo52),
                         "pct_vs_50dma": _pct_from(price, dma50),
                         "pct_vs_200dma": _pct_from(price, dma200),
                         "rsi14": _rsi(close, config.AGENT_RSI_PERIOD),
                         "rel_strength_vs_spy_3m": _rel_strength(close, spy_close, config.AGENT_REL_STRENGTH_LOOKBACK_DAYS),
                         "atr14": atr14, "swing_low_20": swing_low_20, "swing_high_20": swing_high_20},
        "analyst": {"recommendation": (info or {}).get("recommendationKey"),
                    "target_upside_pct": _pct_from(tgt, price),
                    "num_analysts": (info or {}).get("numberOfAnalystOpinions")},
        "insider": {"pct_held_insiders": (info or {}).get("heldPercentInsiders")},
        "news": news_section,
        "peer_relative": {"pe_z": None, "ps_z": None, "ev_ebitda_z": None,
                          "rev_growth_z": None, "gross_margin_z": None},
    }


def _fmt(x, pct=False):
    if x is None:
        return "?"
    return f"{x*100:.0f}%" if pct else f"{x:.1f}"


def compact_line(dossier) -> str:
    v, g, pa = dossier["valuation"], dossier["growth"], dossier["price_action"]
    return (f"{dossier['ticker']} PE:{_fmt(v['pe'])} PS:{_fmt(v['ps'])} "
            f"revG:{_fmt(g['rev_growth'], pct=True)} RSI:{_fmt(pa['rsi14'])} "
            f"vs200dma:{_fmt(pa['pct_vs_200dma'], pct=True)}")
