import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import quant.news.news_store as news_store
class TestNewsStore(unittest.TestCase):

    def setUp(self):
        # Each test gets a fresh isolated DB to prevent cross-test pollution
        self._tmp = tempfile.mkdtemp()
        os.environ["NEWS_DB_PATH"] = os.path.join(self._tmp, "test_news.db")
        news_store.init_db()

    def test_insert_article_returns_id(self):
        aid = news_store.insert_article(
            url="https://example.com/1",
            title="Test Title",
            summary="Test summary",
            source="Test",
            region="us",
            published_at="2026-04-12T08:00:00",
        )
        self.assertIsNotNone(aid)
        self.assertEqual(len(aid), 32)

    def test_insert_article_dedup(self):
        url = "https://example.com/dedup"
        aid1 = news_store.insert_article(url=url, title="A", summary="", source="S", region="us")
        aid2 = news_store.insert_article(url=url, title="A", summary="", source="S", region="us")
        self.assertIsNotNone(aid1)
        self.assertIsNone(aid2)  # duplicate returns None

    def test_insert_and_count_events(self):
        aid = news_store.insert_article(
            url="https://example.com/ev1", title="Tariff news", summary="",
            source="Reuters", region="us"
        )
        news_store.insert_event(aid, "tariff", ["tariff"], 1)
        count = news_store.count_events_in_window("tariff", minutes=60)
        self.assertGreaterEqual(count, 1)

    def test_count_events_excludes_old(self):
        count = news_store.count_events_in_window("geopolitical", minutes=1)
        self.assertEqual(count, 0)

    def test_insert_and_get_analysis(self):
        news_store.insert_analysis(
            trigger="hotspot",
            category="tariff",
            input_summary="test input",
            briefing="Markets fell on tariff news.",
            sector_impacts={"XLY": "bearish"},
            political_risk_score=-0.5,
        )
        latest = news_store.get_latest_analysis()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["trigger"], "hotspot")
        self.assertAlmostEqual(latest["political_risk_score"], -0.5)
        self.assertEqual(latest["sector_impacts"]["XLY"], "bearish")

    def test_get_articles_for_briefing(self):
        aid = news_store.insert_article(
            url="https://example.com/brief1", title="Fed rate cut",
            summary="", source="Reuters", region="us"
        )
        news_store.insert_event(aid, "fed", ["rate cut"], 1)
        articles = news_store.get_articles_for_briefing(hours=8)
        self.assertGreater(len(articles), 0)
        self.assertIn("title", articles[0])
        self.assertIn("category", articles[0])

    def test_cleanup_old_articles(self):
        news_store.insert_article(
            url="https://example.com/fresh", title="Fresh", summary="",
            source="S", region="us"
        )
        news_store.cleanup_old_articles(days=7)
        arts = news_store.get_articles_for_briefing(hours=1)
        self.assertIsInstance(arts, list)

    def test_count_hotspot_llm_calls(self):
        news_store.insert_analysis(
            trigger="hotspot", category="tariff",
            input_summary="x", briefing="y",
            sector_impacts={}, political_risk_score=-0.3,
        )
        count = news_store.count_hotspot_llm_calls("tariff", hours=1)
        self.assertGreaterEqual(count, 1)
        count_other = news_store.count_hotspot_llm_calls("fed", hours=1)
        self.assertEqual(count_other, 0)


if __name__ == "__main__":
    unittest.main()
