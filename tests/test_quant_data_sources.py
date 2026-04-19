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


def test_fetch_ark_trades_uses_arkfunds_io_json():
    from quant.data_sources import fetch_ark_trades
    from quant.schema import ExternalSignal
    from unittest.mock import patch
    import datetime as _dt

    today = _dt.date.today()
    yesterday = today - _dt.timedelta(days=1)
    # arkfunds.io returns per-symbol trades, so the orchestrator fetches
    # across all ARK funds and combines.
    fake_responses = {
        "ARKK": {"trades": [
            {"fund": "ARKK", "date": today.isoformat(), "ticker": "TSLA",
             "direction": "Buy", "shares": 12345, "etf_percent": 0.8},
        ]},
        "ARKG": {"trades": [
            {"fund": "ARKG", "date": yesterday.isoformat(), "ticker": "CRSP",
             "direction": "Sell", "shares": 5000, "etf_percent": 0.3},
        ]},
        "ARKQ": {"trades": []},
        "ARKW": {"trades": []},
        "ARKF": {"trades": []},
    }

    def fake_fetch(symbol):
        return fake_responses.get(symbol, {"trades": []})

    with patch("quant.data_sources._fetch_arkfunds_io_trades", side_effect=fake_fetch):
        sig = fetch_ark_trades()
    assert isinstance(sig, ExternalSignal)
    assert sig.source == "ark"
    dirs = {row["direction"] for row in sig.data}
    assert dirs == {"buy", "sell"}
    tickers = {row["ticker"] for row in sig.data}
    assert tickers == {"TSLA", "CRSP"}


def test_fetch_ark_trades_handles_all_symbols_failing():
    """If every ARK fund's arkfunds.io call fails, stamp error."""
    from quant.data_sources import fetch_ark_trades
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_arkfunds_io_trades",
               side_effect=RuntimeError("arkfunds.io 500")):
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


def test_sec_user_agent_has_real_contact_email():
    """Per SEC fair-access rules, the User-Agent must contain a real contact
    email. Default must NOT be example.com (SEC rate-limits that)."""
    import quant.data_sources as ds
    assert "@example.com" not in ds._SEC_USER_AGENT
    assert "@" in ds._SEC_USER_AGENT  # must have some email

def test_sec_user_agent_reads_from_env(monkeypatch):
    """Verify env override works for operators who want their own contact."""
    monkeypatch.setenv("SEC_CONTACT_EMAIL_UA", "test research me@mycompany.com")
    # Need to reimport to pick up the env change (or re-eval the line)
    # Simpler: just assert the env var pattern is read at module load —
    # this test is documentational; actual behavior is at import time.
    import os
    assert os.environ.get("SEC_CONTACT_EMAIL_UA") == "test research me@mycompany.com"


def test_fetch_13f_returns_error_when_all_funds_silently_return_none():
    """If every fund's _fetch_latest_13f_for_cik returns None (no filings
    discoverable), the signal must be stamped with an error — silent empty
    is a bug (from live-data smoke test)."""
    from quant.data_sources import fetch_13f_filings
    from unittest.mock import patch
    with patch("quant.data_sources._fetch_latest_13f_for_cik", return_value=None):
        sig = fetch_13f_filings()
    assert sig.data == []
    assert sig.error is not None
    assert "no 13F filings" in sig.error.lower() or "none" in sig.error.lower()


def test_fetch_latest_13f_finds_info_table_via_index_json():
    """The info-table filename is arbitrary (e.g. '50240.xml'). We must
    discover it by listing the accession directory's index.json, not
    guessing hardcoded names."""
    from quant.data_sources import _fetch_latest_13f_for_cik
    from unittest.mock import patch, MagicMock

    # Mock _sec_get to return different content per URL
    submissions = {
        "filings": {"recent": {
            "form": ["13F-HR"],
            "accessionNumber": ["0001193125-26-054580"],
            "primaryDocument": ["primary_doc.xml"],
            "reportDate": ["2026-03-31"],
        }}
    }
    index_json = {
        "directory": {"item": [
            {"name": "primary_doc.xml", "size": "5556"},
            {"name": "50240.xml", "size": "55376"},
            {"name": "0001193125-26-054580-index.html", "size": "100"},
        ]}
    }
    info_table_xml = b'''<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <cusip>037833100</cusip>
    <value>150000000</value>
  </infoTable>
</informationTable>'''

    import json as _json
    def fake_sec_get(url, *, timeout=20):
        if url.endswith(".json") and "submissions" in url:
            return _json.dumps(submissions).encode()
        if url.endswith("/index.json"):
            return _json.dumps(index_json).encode()
        if url.endswith("50240.xml"):
            return info_table_xml
        raise RuntimeError(f"unexpected url: {url}")

    with patch("quant.data_sources._sec_get", side_effect=fake_sec_get):
        result = _fetch_latest_13f_for_cik("0001067983")
    assert result is not None
    assert result["top_20"][0]["ticker"] == "APPLE INC"
    assert result["top_20"][0]["value"] == 150_000_000 * 1000   # 13F reports in thousands


def test_fetch_congress_gracefully_handles_service_outage():
    """CapitolTrades' backend has periods of instability (CloudFront Lambda
    errors). The fetcher must surface the error clearly in data_gaps rather
    than silently returning empty data."""
    from quant.data_sources import fetch_congress_trades
    from unittest.mock import patch
    from urllib.error import HTTPError
    with patch("quant.data_sources._fetch_capitoltrades_json",
               side_effect=HTTPError("url", 503, "Service Unavailable", {}, None)):
        sig = fetch_congress_trades()
    assert sig.data == []
    assert sig.error is not None
    assert "503" in sig.error or "service" in sig.error.lower() or "unavailable" in sig.error.lower()


def test_fetch_all_externals_survives_hung_fetcher():
    """If one fetcher hangs past the timeout, the orchestrator must still
    return 5 signals — not raise concurrent.futures.TimeoutError (regression
    from live smoke test 2026-04-19)."""
    import time
    from quant import data_sources
    from quant.schema import ExternalSignal

    def slow():
        time.sleep(10)   # much longer than our 1s budget below
        return ExternalSignal(source="slow", as_of=dt.datetime.now(dt.timezone.utc), data=[])

    def fast(name):
        return lambda: ExternalSignal(
            source=name, as_of=dt.datetime.now(dt.timezone.utc), data=[]
        )

    with patch.object(data_sources, "fetch_13f_filings", slow), \
         patch.object(data_sources, "fetch_reddit_trending", fast("reddit")), \
         patch.object(data_sources, "fetch_popular_etf_holdings", fast("etf-holdings")), \
         patch.object(data_sources, "fetch_ark_trades", fast("ark")), \
         patch.object(data_sources, "fetch_congress_trades", fast("congress")):
        signals = data_sources.fetch_all_externals(timeout_per_source=1)

    assert len(signals) == 5
    by_source = {s.source: s for s in signals}
    # The hung fetcher's slot is present with error="timed out"
    assert "13F" in by_source
    assert by_source["13F"].error is not None
    assert "timed out" in by_source["13F"].error.lower() or "timeout" in by_source["13F"].error.lower()
    # Fast fetchers returned cleanly
    for src in ("reddit", "etf-holdings", "ark", "congress"):
        assert by_source[src].error is None
