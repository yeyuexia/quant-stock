import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

_tmp = tempfile.mkdtemp()
os.environ["NEWS_DB_PATH"] = os.path.join(_tmp, "test_political.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import quant.news.news_store as news_store
news_store.init_db()

import quant.news.political as political
class TestCategorizeArticle(unittest.TestCase):

    def test_tariff_keyword(self):
        cat, kws, sev = political.categorize_article("New tariff on China imports", "")
        self.assertEqual(cat, "tariff")
        self.assertIn("tariff", kws)
        self.assertEqual(sev, 1)

    def test_fed_keyword(self):
        cat, kws, sev = political.categorize_article("FOMC raises interest rate by 25 basis points", "")
        self.assertEqual(cat, "fed")

    def test_geopolitical_keyword(self):
        cat, kws, sev = political.categorize_article("Military conflict escalates near Taiwan", "")
        self.assertEqual(cat, "geopolitical")

    def test_no_match_returns_none(self):
        cat, kws, sev = political.categorize_article("Local bakery wins award", "")
        self.assertIsNone(cat)
        self.assertEqual(kws, [])
        self.assertEqual(sev, 0)

    def test_multi_category_raises_severity(self):
        # Both tariff and geopolitical keywords
        cat, kws, sev = political.categorize_article(
            "Tariff sanctions escalate military conflict", ""
        )
        self.assertIsNotNone(cat)
        self.assertEqual(sev, 2)  # 2+ categories matched


class TestFetchRssFeed(unittest.TestCase):

    @patch("quant.news.political.feedparser.parse")
    @patch("quant.news.political.urllib.request.urlopen")
    def test_returns_articles(self, mock_urlopen, mock_parse):
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        mock_parse.return_value = MagicMock(bozo=False, entries=[
            MagicMock(
                link="https://example.com/1",
                title="Test Article",
                summary="Summary text",
                description="",
                published="2026-04-12",
            )
        ])
        articles = political.fetch_rss_feed("https://fake.url", "TestSource", "us")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["source"], "TestSource")
        self.assertEqual(articles[0]["region"], "us")

    @patch("quant.news.political.urllib.request.urlopen", side_effect=Exception("network error"))
    def test_returns_empty_on_error(self, mock_urlopen):
        articles = political.fetch_rss_feed("https://fake.url", "S", "us")
        self.assertEqual(articles, [])


class TestRunClassificationPass(unittest.TestCase):

    @patch("quant.news.political.fetch_all_rss")
    def test_stores_new_articles(self, mock_fetch):
        mock_fetch.return_value = [{
            "url": "https://example.com/tariff99",
            "title": "New tariff on imports announced",
            "summary": "",
            "source": "Reuters",
            "region": "us",
            "published_at": "",
        }]
        political.run_classification_pass()
        count = news_store.count_events_in_window("tariff", minutes=60)
        self.assertGreaterEqual(count, 1)

    @patch("quant.news.political.fetch_all_rss")
    def test_deduplicates_on_second_pass(self, mock_fetch):
        article = {
            "url": "https://example.com/dedup99",
            "title": "Tariff update",
            "summary": "",
            "source": "Reuters",
            "region": "us",
            "published_at": "",
        }
        mock_fetch.return_value = [article]
        political.run_classification_pass()
        before = news_store.count_events_in_window("tariff", minutes=60)
        political.run_classification_pass()
        after = news_store.count_events_in_window("tariff", minutes=60)
        self.assertEqual(before, after)  # second pass adds nothing


if __name__ == "__main__":
    unittest.main()
