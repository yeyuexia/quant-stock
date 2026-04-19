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
