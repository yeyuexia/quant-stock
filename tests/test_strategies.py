import json
import strategies


def test_write_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    rows = [{"ticker": "AAPL", "score": 1.2, "rank": 1, "factors": {"v": 0.3}}]
    path = strategies.write_strategy_result("value", rows)
    assert path.endswith("value.json")
    loaded = strategies.load_strategy_results()
    assert loaded["value"]["strategy"] == "value"
    assert loaded["value"]["rows"][0]["ticker"] == "AAPL"
    assert "generated_at" in loaded["value"]


def test_load_skips_corrupt_file(tmp_path, monkeypatch):
    d = tmp_path / "strat"
    d.mkdir()
    (d / "value.json").write_text('{"strategy": "value", "rows": []}')
    (d / "canslim.json").write_text("{ broken json")
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(d))
    loaded = strategies.load_strategy_results()
    assert "value" in loaded
    assert "canslim" not in loaded   # corrupt file skipped, no crash


def test_run_strategies_isolates_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(strategies, "STRATEGIES_DIR", str(tmp_path / "strat"))
    def good():
        return [{"ticker": "MSFT", "score": 2.0, "rank": 1, "factors": {}}]
    def bad():
        raise RuntimeError("boom")
    paths = strategies.run_strategies({"value": good, "canslim": bad})
    assert any(p.endswith("value.json") for p in paths)
    loaded = strategies.load_strategy_results()
    assert "value" in loaded and "canslim" not in loaded


def test_canslim_adapter_maps_dataframe_rows(monkeypatch):
    import pandas as pd
    import strategies as S
    df = pd.DataFrame([
        {"ticker": "AAA", "composite": 9.0, "rs": 95},
        {"ticker": "BBB", "composite": 7.0, "rs": 80},
    ])
    monkeypatch.setattr("screener.screen_stocks", lambda: df)
    rows = S._canslim_rows()
    assert rows[0] == {"ticker": "AAA", "score": 9.0, "rank": 1,
                       "factors": {"composite": 9.0, "rs": 95}}
    assert rows[1]["rank"] == 2


def test_default_registry_has_configured_strategies():
    import strategies as S
    reg = S.default_registry()
    assert set(reg) == {"value", "canslim"}
    assert all(callable(fn) for fn in reg.values())
