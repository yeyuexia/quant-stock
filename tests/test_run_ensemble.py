"""Tests for the ensemble entrypoint (strategies -> agent pick)."""
import run_ensemble


def test_run_invokes_strategies_then_select(monkeypatch):
    calls = []
    monkeypatch.setattr(run_ensemble.strategies, "default_registry",
                        lambda: {"value": lambda: []})
    monkeypatch.setattr(run_ensemble.strategies, "run_strategies",
                        lambda reg: calls.append("strat") or ["value.json"])
    monkeypatch.setattr(
        run_ensemble.investor_agent, "select_candidates",
        lambda: (calls.append("select")
                 or [{"ticker": "AAA", "rationale": "x", "strategies": ["value"]}]))
    picks = run_ensemble.run()
    assert calls == ["strat", "select"]          # strategies first, then agent
    assert picks[0]["ticker"] == "AAA"
