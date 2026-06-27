import value_screen


def _info(price, mcap, advol, fcf, roe, d2e, fpe, p2b, ev_ebitda):
    return {
        "currentPrice": price, "marketCap": mcap, "averageVolume": advol,
        "freeCashflow": fcf, "returnOnEquity": roe, "debtToEquity": d2e,
        "forwardPE": fpe, "priceToBook": p2b, "enterpriseToEbitda": ev_ebitda,
    }


def _make_info_fn(table):
    return lambda t: table.get(t, {})


def test_gates_exclude_illiquid_cheap_microcap(monkeypatch):
    table = {
        # below price floor ($5)
        "PENNY": _info(3.0, 1e9, 1e6, 1e8, 0.2, 50, 12, 3, 10),
        # below dollar-volume gate (price*advol = 10*100 = 1000)
        "ILLQ": _info(10.0, 1e9, 100, 1e8, 0.2, 50, 12, 3, 10),
        # below market cap floor
        "MICRO": _info(10.0, 1e8, 1e6, 1e8, 0.2, 50, 12, 3, 10),
        # negative FCF AND negative ROE (trap)
        "JUNK": _info(10.0, 1e9, 1e6, -1e8, -0.2, 50, 12, 3, 10),
        # clean
        "GOOD": _info(50.0, 5e9, 1e6, 5e8, 0.25, 30, 10, 2, 8),
    }
    rows = value_screen.screen_value_quality(
        list(table), info_fn=_make_info_fn(table),
        fund_fn=lambda t: {}, price_fn=lambda t: None)
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["GOOD"]   # only the clean one survives the gates


def test_cheaper_higher_quality_ranks_first(monkeypatch):
    # Two survivors; CHEAP has higher FCF yield + ROE → higher composite.
    table = {
        "CHEAP": _info(20.0, 1e9, 1e6, 2e8, 0.30, 10, 8, 1.0, 6),
        "RICH":  _info(20.0, 1e9, 1e6, 2e7, 0.05, 200, 40, 6.0, 30),
    }
    rows = value_screen.screen_value_quality(
        ["CHEAP", "RICH"], info_fn=_make_info_fn(table),
        fund_fn=lambda t: {}, price_fn=lambda t: None)
    assert [r["ticker"] for r in rows] == ["CHEAP", "RICH"]
    assert rows[0]["rank"] == 1
    assert rows[0]["score"] > rows[1]["score"]


def test_fail_open_on_empty_info(monkeypatch):
    rows = value_screen.screen_value_quality(
        ["X"], info_fn=lambda t: {}, fund_fn=lambda t: {}, price_fn=lambda t: None)
    assert rows == []   # no data → excluded, no crash
