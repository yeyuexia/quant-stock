from value_fundamentals import from_info, Fundamentals


def test_from_info_maps_and_normalizes():
    info = {"trailingEps": 3.2, "marketCap": 5e9, "trailingPE": 14.0, "pegRatio": 0.8,
            "enterpriseToEbitda": 9.0, "priceToSalesTrailing12Months": 2.0,
            "revenueGrowth": 0.22, "earningsGrowth": 0.18, "grossMargins": 0.41,
            "operatingMargins": 0.12, "debtToEquity": 80.0, "currentRatio": 2.1,
            "freeCashflow": 4e8, "totalCash": 1e9}
    f = from_info("AAA", info)
    assert f.ticker == "AAA"
    assert f.is_profitable is True
    assert f.pe == 14.0 and f.peg == 0.8 and f.ps == 2.0
    assert abs(f.debt_equity - 0.8) < 1e-9         # percent → ratio
    assert f.gross_margin == 0.41 and f.fcf == 4e8


def test_from_info_unprofitable_and_missing():
    f = from_info("BBB", {"netIncomeToCommon": -2e8, "priceToSalesTrailing12Months": 5.0})
    assert f.is_profitable is False
    assert f.ps == 5.0
    assert f.pe is None and f.peg is None and f.debt_equity is None  # absent → None


def test_from_info_empty_does_not_crash():
    f = from_info("CCC", {})
    assert f.is_profitable is False and f.market_cap is None
