"""Normalize a raw yfinance .info dict into a typed, missing-aware Fundamentals
record. The ONE place that knows yfinance's key names + None/NaN handling, so
the rest of the value screen sees clean fields. Pure — no network."""
from dataclasses import dataclass
from typing import Optional


def _num(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
        return float(v)
    return None


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    market_cap: Optional[float]
    is_profitable: bool
    pe: Optional[float]
    peg: Optional[float]
    ev_ebitda: Optional[float]
    ps: Optional[float]
    rev_growth: Optional[float]
    eps_growth: Optional[float]
    gross_margin: Optional[float]
    op_margin: Optional[float]
    debt_equity: Optional[float]
    current_ratio: Optional[float]
    fcf: Optional[float]
    total_cash: Optional[float]


def from_info(ticker: str, info: dict) -> Fundamentals:
    info = info or {}
    eps = _num(info.get("trailingEps"))
    ni = _num(info.get("netIncomeToCommon"))
    is_profitable = (eps is not None and eps > 0) or (ni is not None and ni > 0)
    d2e = _num(info.get("debtToEquity"))
    if d2e is not None:
        d2e = d2e / 100.0          # yfinance reports a percent (80 = 0.8x)
    return Fundamentals(
        ticker=ticker,
        market_cap=_num(info.get("marketCap")),
        is_profitable=is_profitable,
        pe=_num(info.get("trailingPE")),
        peg=_num(info.get("pegRatio")),
        ev_ebitda=_num(info.get("enterpriseToEbitda")),
        ps=_num(info.get("priceToSalesTrailing12Months")),
        rev_growth=_num(info.get("revenueGrowth")),
        eps_growth=_num(info.get("earningsGrowth")),
        gross_margin=_num(info.get("grossMargins")),
        op_margin=_num(info.get("operatingMargins")),
        debt_equity=d2e,
        current_ratio=_num(info.get("currentRatio")),
        fcf=_num(info.get("freeCashflow")),
        total_cash=_num(info.get("totalCash")),
    )
