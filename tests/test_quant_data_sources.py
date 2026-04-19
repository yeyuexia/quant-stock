import datetime as dt
from unittest.mock import patch, MagicMock


def test_fetch_13f_returns_external_signal_on_empty_response():
    """When SEC endpoints return nothing, we get an ExternalSignal with
    data=[] — never a raise."""
    from quant.data_sources import fetch_13f_filings
    from quant.schema import ExternalSignal

    with patch("quant.data_sources._fetch_latest_13f_for_cik", return_value=None):
        sig = fetch_13f_filings()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "13F"
    assert sig.data == []


def test_fetch_13f_aggregates_across_funds():
    from quant.data_sources import fetch_13f_filings

    def fake_fetch(cik):
        mapping = {
            "0001067983": {
                "period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "AAPL", "value": 150_000_000, "weight": 0.23}],
            },
            "0001336528": {
                "period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "MSFT", "value": 80_000_000, "weight": 0.08}],
            },
        }
        return mapping.get(cik)

    with patch("quant.data_sources._fetch_latest_13f_for_cik", side_effect=fake_fetch), \
         patch("quant.data_sources._TRACKED_13F_FUNDS", {
             "0001067983": "Berkshire Hathaway",
             "0001336528": "Bridgewater",
         }):
        sig = fetch_13f_filings()
    assert sig.source == "13F"
    assert sig.error is None
    symbols = {row["ticker"] for row in sig.data}
    assert symbols == {"AAPL", "MSFT"}
    funds = {row["fund"] for row in sig.data}
    assert funds == {"Berkshire Hathaway", "Bridgewater"}


def test_fetch_13f_tolerates_per_fund_errors():
    """If one fund's fetch fails, others still return."""
    from quant.data_sources import fetch_13f_filings

    def fake_fetch(cik):
        if cik == "broken":
            raise RuntimeError("SEC returned 500")
        return {"period_of_report": dt.date(2025, 12, 31),
                "top_20": [{"ticker": "AAPL", "value": 1, "weight": 0.01}]}

    with patch("quant.data_sources._fetch_latest_13f_for_cik", side_effect=fake_fetch), \
         patch("quant.data_sources._TRACKED_13F_FUNDS", {
             "0001067983": "Berkshire",
             "broken": "BrokenFund",
         }):
        sig = fetch_13f_filings()
    assert any(row["fund"] == "Berkshire" for row in sig.data)
    assert not any(row["fund"] == "BrokenFund" for row in sig.data)


def test_fetch_13f_returns_error_signal_on_total_failure():
    """If ALL funds fail, return an ExternalSignal with error set."""
    from quant.data_sources import fetch_13f_filings

    with patch("quant.data_sources._fetch_latest_13f_for_cik",
               side_effect=RuntimeError("network down")):
        sig = fetch_13f_filings()
    assert sig.data == []
    assert sig.error is not None


def test_fetch_reddit_trending_returns_tickers():
    from quant.data_sources import fetch_reddit_trending
    from quant.schema import ExternalSignal

    fake_posts = [
        {"title": "NVDA to the moon!", "score": 500, "ts": 1713500000,
         "subreddit": "wallstreetbets"},
        {"title": "Bought more $TSLA calls, going long", "score": 200, "ts": 1713500100,
         "subreddit": "wallstreetbets"},
        {"title": "Shorting AAPL before earnings", "score": 50, "ts": 1713500200,
         "subreddit": "stocks"},
    ]
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_reddit_hot_posts", return_value=fake_posts):
        sig = fetch_reddit_trending()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "reddit"
    tickers = {row["ticker"] for row in sig.data}
    assert "NVDA" in tickers
    assert "TSLA" in tickers


def test_fetch_reddit_trending_handles_network_failure():
    from quant.data_sources import fetch_reddit_trending
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_reddit_hot_posts",
               side_effect=RuntimeError("blocked")):
        sig = fetch_reddit_trending()
    assert sig.data == []
    assert sig.error is not None


def test_fetch_etf_holdings_normalizes_rows():
    from quant.data_sources import fetch_popular_etf_holdings
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import pandas as pd

    fake_holdings = pd.DataFrame({
        "symbol": ["AAPL", "MSFT", "NVDA"],
        "holdingPercent": [0.15, 0.12, 0.08],
    })
    with patch("quant.data_sources._fetch_etf_top_holdings", return_value=fake_holdings):
        sig = fetch_popular_etf_holdings()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "etf-holdings"
    first = sig.data[0]
    assert "etf" in first and "ticker" in first and "weight" in first


def test_fetch_etf_holdings_tolerates_missing_etfs():
    from quant.data_sources import fetch_popular_etf_holdings
    from unittest.mock import patch

    def fake_fetch(symbol):
        if symbol == "ARKK":
            raise RuntimeError("not found")
        import pandas as pd
        return pd.DataFrame({"symbol": ["FOO"], "holdingPercent": [0.1]})

    with patch("quant.data_sources._fetch_etf_top_holdings", side_effect=fake_fetch):
        sig = fetch_popular_etf_holdings()
    assert any(row["ticker"] == "FOO" for row in sig.data)
    assert sig.error is None or "ARKK" in sig.error


def test_fetch_ark_trades_parses_csv():
    from quant.data_sources import fetch_ark_trades
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import datetime as _dt

    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)
    fake_csv = (
        "date,fund,direction,ticker,company,shares,weight(%)\n"
        f"{today.month}/{today.day}/{today.year},ARKK,Buy,TSLA,TESLA INC,12345,0.8\n"
        f"{yesterday.month}/{yesterday.day}/{yesterday.year},ARKG,Sell,CRSP,CRISPR THERA,5000,0.3\n"
    )
    with patch("quant.data_sources._fetch_ark_csv", return_value=fake_csv):
        sig = fetch_ark_trades()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "ark"
    dirs = {row["direction"] for row in sig.data}
    assert dirs == {"buy", "sell"}
    tickers = {row["ticker"] for row in sig.data}
    assert tickers == {"TSLA", "CRSP"}


def test_fetch_ark_trades_handles_fetch_failure():
    from quant.data_sources import fetch_ark_trades
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_ark_csv", side_effect=RuntimeError("404")):
        sig = fetch_ark_trades()
    assert sig.data == []
    assert sig.error is not None


def test_fetch_congress_trades_parses_json():
    from quant.data_sources import fetch_congress_trades
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import datetime as _dt

    today = _dt.date.today()
    recent = (today - _dt.timedelta(days=2)).isoformat()
    traded = (today - _dt.timedelta(days=4)).isoformat()

    fake_json = {
        "data": [
            {"politician": {"firstName": "Nancy", "lastName": "Pelosi"},
             "traded": traded, "disclosed": recent,
             "asset": {"ticker": "TSLA"},
             "type": "buy",
             "value": "$1,000,001 - $5,000,000"},
            {"politician": {"firstName": "Josh", "lastName": "Gottheimer"},
             "traded": traded, "disclosed": recent,
             "asset": {"ticker": "NVDA"},
             "type": "sell",
             "value": "$50,001 - $100,000"},
        ]
    }
    with patch("quant.data_sources._fetch_capitoltrades_json", return_value=fake_json):
        sig = fetch_congress_trades()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "congress"
    tickers = {row["ticker"] for row in sig.data}
    assert tickers == {"TSLA", "NVDA"}
    pelosi_row = next(r for r in sig.data if r["member"] == "Nancy Pelosi")
    assert pelosi_row["direction"] == "buy"


def test_fetch_congress_trades_handles_network_error():
    from quant.data_sources import fetch_congress_trades
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_capitoltrades_json",
               side_effect=RuntimeError("timeout")):
        sig = fetch_congress_trades()
    assert sig.data == []
    assert sig.error is not None


def test_fetch_all_externals_returns_five_signals():
    from quant.data_sources import fetch_all_externals
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import datetime as _dt

    def stub(source):
        return lambda: ExternalSignal(
            source=source,
            as_of=_dt.datetime.now(_dt.timezone.utc),
            data=[{"row": source}],
        )

    with patch("quant.data_sources.fetch_13f_filings", side_effect=stub("13F")), \
         patch("quant.data_sources.fetch_reddit_trending", side_effect=stub("reddit")), \
         patch("quant.data_sources.fetch_popular_etf_holdings",
               side_effect=stub("etf-holdings")), \
         patch("quant.data_sources.fetch_ark_trades", side_effect=stub("ark")), \
         patch("quant.data_sources.fetch_congress_trades", side_effect=stub("congress")):
        signals = fetch_all_externals()
    assert len(signals) == 5
    sources = {s.source for s in signals}
    assert sources == {"13F", "reddit", "etf-holdings", "ark", "congress"}


def test_fetch_all_externals_catches_fetcher_crash():
    """If a fetcher raises (not just returns an error signal), we still get
    back 5 signals — the crashed one has error populated."""
    from quant.data_sources import fetch_all_externals
    from unittest.mock import patch
    with patch("quant.data_sources.fetch_13f_filings", side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_reddit_trending",
               side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_popular_etf_holdings",
               side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_ark_trades", side_effect=RuntimeError("boom")), \
         patch("quant.data_sources.fetch_congress_trades",
               side_effect=RuntimeError("boom")):
        signals = fetch_all_externals()
    assert len(signals) == 5
    for s in signals:
        assert s.error is not None
        assert s.data == []
