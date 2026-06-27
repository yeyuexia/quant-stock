import value_screen


def _info(profitable=True, **kw):
    base = {"marketCap": 5e9, "trailingEps": 3.0 if profitable else -1.0,
            "trailingPE": 14.0, "pegRatio": 0.8, "priceToSalesTrailing12Months": 2.0,
            "revenueGrowth": 0.2, "earningsGrowth": 0.15, "grossMargins": 0.4,
            "debtToEquity": 50.0, "currentRatio": 2.0, "freeCashflow": 4e8, "totalCash": 1e9}
    base.update(kw); return base


def test_screen_emits_twotrack_rows():
    prices = {t: (50.0, 9_000_000) for t in ("AAA", "BBB", "JUNK")}
    infos = {
        "AAA": _info(profitable=True),
        "BBB": _info(profitable=False, priceToSalesTrailing12Months=4.0,
                     revenueGrowth=0.4, grossMargins=0.5, freeCashflow=-2e8, totalCash=5e9),
        "JUNK": _info(profitable=True, pegRatio=3.0, trailingPE=40.0),  # fails track A gates
    }
    rows = value_screen.screen(["AAA", "BBB", "JUNK"],
                               price_fn=lambda ts: prices, info_fn=lambda t: infos[t])
    tickers = [r["ticker"] for r in rows]
    assert "AAA" in tickers and "BBB" in tickers and "JUNK" not in tickers
    assert {r["factors"]["track"] for r in rows} <= {"A", "B"}
    assert rows[0]["rank"] == 1 and all("score" in r for r in rows)


def test_screen_empty_universe_returns_empty():
    assert value_screen.screen([], price_fn=lambda ts: {}, info_fn=lambda t: {}) == []


def test_run_writes_strategy_result(tmp_path, monkeypatch):
    import strategies
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    monkeypatch.setattr(value_screen.discovery, "get_russell3000_tickers", lambda: ["AAA"])
    monkeypatch.setattr(value_screen, "screen", lambda u, **k: [
        {"ticker": "AAA", "score": 1.0, "rank": 1, "factors": {"track": "A"}}])
    rows = value_screen.run()
    assert rows[0]["ticker"] == "AAA"
    assert strategies.load_strategy_results()["value"]["rows"][0]["ticker"] == "AAA"
