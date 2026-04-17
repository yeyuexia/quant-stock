# tests/test_fakes_smoke.py
import datetime as dt
from tests.fakes import FakeMarketData, FakeNewsFeed


def test_fake_market_data_spy_default():
    md = FakeMarketData()
    assert md.spy_at(dt.datetime(2026, 4, 17)) == 480.0


def test_fake_market_data_seeded():
    md = FakeMarketData(
        spy_by_time={dt.datetime(2026, 4, 17, 14): 475.0},
    )
    assert md.spy_at(dt.datetime(2026, 4, 17, 14)) == 475.0


def test_fake_news_feed_fetch_since():
    fd = FakeNewsFeed()
    fd.add(title="Old news", source="x", ts=dt.datetime(2026, 4, 17, 9, 0))
    fd.add(title="Fresh news", source="y", ts=dt.datetime(2026, 4, 17, 14, 0))
    out = fd.fetch_since(dt.datetime(2026, 4, 17, 10, 0))
    assert len(out) == 1
    assert out[0]["title"] == "Fresh news"
