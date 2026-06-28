import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import quant.news.news_store as news_store
import quant.news.forecast as forecast
class TestAnalyzeHotspot(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["NEWS_DB_PATH"] = os.path.join(self._tmp, "test_forecast.db")
        news_store.init_db()

    @patch("quant.news.forecast._call_claude")
    def test_returns_analysis_dict(self, mock_call):
        mock_call.return_value = (
            '{"summary": "Tariffs hit tech stocks.", '
            '"sector_impacts": {"XLY": "bearish"}, '
            '"confidence": "high", '
            '"political_risk_score": -0.6}'
        )
        result = forecast.analyze_hotspot("tariff", [{"title": "New tariffs announced"}])
        self.assertEqual(result["confidence"], "high")
        self.assertAlmostEqual(result["political_risk_score"], -0.6)
        self.assertEqual(result["sector_impacts"]["XLY"], "bearish")

    @patch("quant.news.forecast._call_claude")
    def test_handles_invalid_json(self, mock_call):
        mock_call.return_value = "not json at all"
        result = forecast.analyze_hotspot("tariff", [{"title": "headline"}])
        self.assertIn("summary", result)
        self.assertEqual(result["political_risk_score"], 0.0)

    @patch("quant.news.forecast._call_claude")
    def test_handles_markdown_fences(self, mock_call):
        mock_call.return_value = (
            '```json\n{"summary": "Fed cuts.", "sector_impacts": {}, '
            '"confidence": "medium", "political_risk_score": 0.4}\n```'
        )
        result = forecast.analyze_hotspot("fed", [{"title": "Fed cuts rates"}])
        self.assertAlmostEqual(result["political_risk_score"], 0.4)

    @patch("quant.news.forecast._call_claude")
    def test_stores_analysis_in_db(self, mock_call):
        mock_call.return_value = (
            '{"summary": "Fed cuts.", "sector_impacts": {}, '
            '"confidence": "medium", "political_risk_score": 0.4}'
        )
        forecast.analyze_hotspot("fed", [{"title": "Fed cuts rates"}])
        latest = news_store.get_latest_analysis()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["trigger"], "hotspot")
        self.assertEqual(latest["category"], "fed")

    @patch("quant.news.forecast._call_claude", side_effect=RuntimeError("CLI not found"))
    def test_handles_cli_error(self, mock_call):
        result = forecast.analyze_hotspot("tariff", [{"title": "headline"}])
        self.assertEqual(result["political_risk_score"], 0.0)
        self.assertEqual(result["confidence"], "low")


class TestGetLatestPoliticalScore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["NEWS_DB_PATH"] = os.path.join(self._tmp, "test_forecast2.db")
        news_store.init_db()

    def test_returns_zero_when_no_analysis(self):
        score = forecast.get_latest_political_score()
        self.assertIsInstance(score, float)
        self.assertEqual(score, 0.0)

    @patch("quant.news.forecast._call_claude")
    def test_returns_stored_score(self, mock_call):
        mock_call.return_value = (
            '{"summary": "x", "sector_impacts": {}, '
            '"confidence": "low", "political_risk_score": -0.8}'
        )
        forecast.analyze_hotspot("geopolitical", [{"title": "War escalates"}])
        score = forecast.get_latest_political_score()
        self.assertAlmostEqual(score, -0.8)


if __name__ == "__main__":
    unittest.main()
