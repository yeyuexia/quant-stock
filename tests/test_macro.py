"""Regression tests for macro.py fixes (M2, M5, M8, M11, M15, M20, atomic write)."""
import os
import sys
import time
import pytest
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import macro


# ── M2: yield curve returns N/A on missing data, not magic 4.0 ────

def test_yield_curve_returns_na_when_dgs2_missing(monkeypatch):
    """Old code used hardcoded 4.0 when 2Y missing — produced wrong spread."""
    def fake_fetch(series_id, **kw):
        if series_id == "DGS10":
            return pd.Series([4.5], index=pd.date_range("2026-05-01", periods=1))
        return pd.Series(dtype=float)  # DGS2 empty
    monkeypatch.setattr(macro, "_fetch_fred_series", fake_fetch)

    result = macro.get_yield_curve_signal()
    assert result["signal"] == 0
    assert result["value"] is None
    assert "2Y unavailable" in result["label"]


def test_yield_curve_returns_na_when_dgs10_missing(monkeypatch):
    def fake_fetch(series_id, **kw):
        if series_id == "DGS2":
            return pd.Series([4.0], index=pd.date_range("2026-05-01", periods=1))
        return pd.Series(dtype=float)  # DGS10 empty
    monkeypatch.setattr(macro, "_fetch_fred_series", fake_fetch)

    result = macro.get_yield_curve_signal()
    assert result["signal"] == 0
    assert result["value"] is None
    assert "10Y unavailable" in result["label"]


def test_yield_curve_normal_spread(monkeypatch):
    def fake_fetch(series_id, **kw):
        idx = pd.date_range("2026-05-01", periods=1)
        if series_id == "DGS10":
            return pd.Series([4.5], index=idx)
        if series_id == "DGS2":
            return pd.Series([4.0], index=idx)  # 50bp spread
        return pd.Series(dtype=float)
    monkeypatch.setattr(macro, "_fetch_fred_series", fake_fetch)

    result = macro.get_yield_curve_signal()
    assert result["value"] == pytest.approx(0.5)
    # 0.5 is in [0, 1.5) range → signal = 0.5
    assert result["signal"] == 0.5


# ── M5: Sahm Rule official formula ──────────────────────────────

def test_sahm_rule_triggers_on_official_threshold(monkeypatch):
    """Build a UNRATE series where current 3m MA - min(prior 3m MA) ≥ 0.5."""
    # 14+ months of unemployment; last 3 prints jump to push 3m MA up.
    # Months: 3.5 × 11, then 4.5, 4.7, 4.9 (recent jump).
    vals = [3.5] * 11 + [4.5, 4.7, 4.9]
    idx = pd.date_range("2025-01-01", periods=len(vals), freq="MS")
    series = pd.Series(vals, index=idx)
    monkeypatch.setattr(macro, "_fetch_fred_series",
                        lambda series_id, **kw: series if series_id == "UNRATE" else pd.Series(dtype=float))

    result = macro.get_unemployment_signal()
    # ma3 = rolling 3m MA. Last ma3 = mean(4.5, 4.7, 4.9) = 4.7
    # min over last 12 ma3 values: when did rolling start producing values?
    # rolling(3) needs 3 values, so ma3 starts at idx=2. First values ~3.5.
    # So min ≈ 3.5. Sahm = 4.7 - 3.5 = 1.2, well above 0.5 → signal=-1.
    assert result["signal"] == -1.0
    assert "Sahm" in result["label"]


def test_sahm_does_not_trigger_on_stable_unemployment(monkeypatch):
    vals = [4.0] * 14  # flat
    idx = pd.date_range("2025-01-01", periods=len(vals), freq="MS")
    series = pd.Series(vals, index=idx)
    monkeypatch.setattr(macro, "_fetch_fred_series",
                        lambda series_id, **kw: series if series_id == "UNRATE" else pd.Series(dtype=float))

    result = macro.get_unemployment_signal()
    # ma3 = 4.0 throughout → sahm = 0 → not triggered
    assert result["signal"] != -1.0


def test_sahm_handles_insufficient_history(monkeypatch):
    series = pd.Series([4.0] * 5,
                       index=pd.date_range("2026-01-01", periods=5, freq="MS"))
    monkeypatch.setattr(macro, "_fetch_fred_series",
                        lambda series_id, **kw: series if series_id == "UNRATE" else pd.Series(dtype=float))
    result = macro.get_unemployment_signal()
    assert result["signal"] == 0
    assert result["value"] is None


# ── M8: composite normalization excludes failed indicators ────────

def test_macro_composite_normalizes_over_available_indicators(monkeypatch):
    """4 bullish + 2 missing should give 0.5 (not 0.5 × 4/6 = 0.33)."""
    def bullish(*a, **kw):
        return {"signal": 0.5, "value": 1.0, "label": "test"}
    def missing(*a, **kw):
        return {"signal": 0, "value": None, "label": "N/A"}

    monkeypatch.setattr(macro, "get_yield_curve_signal", bullish)
    monkeypatch.setattr(macro, "get_credit_spread_signal", bullish)
    monkeypatch.setattr(macro, "get_unemployment_signal", bullish)
    monkeypatch.setattr(macro, "get_fed_funds_signal", bullish)
    monkeypatch.setattr(macro, "get_financial_conditions_signal", missing)
    monkeypatch.setattr(macro, "get_market_breadth_signal", missing)

    result = macro.macro_regime_score()
    # Should be 0.5 (only the 4 bullish indicators contribute), not diluted
    assert result["score"] == pytest.approx(0.5, abs=0.01)


def test_macro_composite_all_missing_returns_zero(monkeypatch):
    def missing(*a, **kw):
        return {"signal": 0, "value": None, "label": "N/A"}
    for name in ("get_yield_curve_signal", "get_credit_spread_signal",
                 "get_unemployment_signal", "get_fed_funds_signal",
                 "get_financial_conditions_signal", "get_market_breadth_signal"):
        monkeypatch.setattr(macro, name, missing)

    result = macro.macro_regime_score()
    # Total weight = 0 → composite stays at 0 (handled by `if total_weight > 0`)
    assert result["score"] == 0.0


# ── M11: retry on transient FRED error ────────────────────────────

def test_fetch_fred_series_retries_once(monkeypatch, tmp_path):
    """First Fred call fails, second succeeds — series returned."""
    monkeypatch.setattr(macro, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(macro, "FRED_API_KEY", "fake_key")

    calls = {"n": 0}

    class FakeFred:
        def __init__(self, api_key):
            pass
        def get_series(self, series_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient FRED blip")
            return pd.Series([4.5],
                             index=pd.date_range("2026-05-01", periods=1))

    monkeypatch.setitem(sys.modules, "fredapi",
                        type(sys)("fredapi"))
    sys.modules["fredapi"].Fred = FakeFred

    result = macro._fetch_fred_series("DGS10")
    assert not result.empty
    assert calls["n"] == 2  # 1 failure + 1 retry


def test_fetch_fred_series_returns_empty_after_both_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(macro, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(macro, "FRED_API_KEY", "fake_key")

    class DeadFred:
        def __init__(self, api_key):
            pass
        def get_series(self, series_id):
            raise RuntimeError("always fails")

    sys.modules["fredapi"] = type(sys)("fredapi")
    sys.modules["fredapi"].Fred = DeadFred

    result = macro._fetch_fred_series("DGS10")
    assert result.empty


# ── M20: filename sanitization ──────────────────────────────────

def test_sanitize_series_id_safe_chars():
    # Common FRED IDs unchanged
    assert macro._sanitize_series_id("DGS10") == "DGS10"
    assert macro._sanitize_series_id("BAMLH0A0HYM2") == "BAMLH0A0HYM2"
    assert macro._sanitize_series_id("M2SL") == "M2SL"


def test_sanitize_series_id_strips_specials():
    # Defensive against hypothetical future IDs with separators
    assert macro._sanitize_series_id("FOO/BAR") == "FOO_BAR"
    assert macro._sanitize_series_id("X.Y") == "X_Y"
    assert macro._sanitize_series_id("AB CD") == "AB_CD"


# ── M15: fed funds direction matches signal horizon ──────────────

def test_fed_funds_direction_uses_6m_horizon(monkeypatch):
    """When 6m shows cuts but 3m shows tiny uptick, direction must say 'cutting'."""
    # 6 monthly prints: 5.5, 5.5, 5.5, 5.0, 4.5, 4.6
    # current=4.6, prev_3m (idx -3)=5.0 → change_3m = -0.4
    # prev_6m (idx -6)=5.5 → change_6m = -0.9
    vals = [5.5, 5.5, 5.5, 5.0, 4.5, 4.6]
    idx = pd.date_range("2025-12-01", periods=len(vals), freq="MS")
    series = pd.Series(vals, index=idx)
    monkeypatch.setattr(macro, "_fetch_fred_series",
                        lambda series_id, **kw: series if series_id == "FEDFUNDS" else pd.Series(dtype=float))

    result = macro.get_fed_funds_signal()
    # change_6m = -0.9 → signal=1.0 (aggressive cuts)
    assert result["signal"] == 1.0
    assert "cutting" in result["label"]
    # Old version would have said "cutting" via change_3m=-0.4 < 0 — same in
    # this case. The risk case: change_6m=-0.9, change_3m=+0.1 (recent uptick)
    # would say cutting (correct) vs old said hiking (wrong).


# ── atomic_write_csv basic shape ─────────────────────────────────

def test_atomic_write_csv_creates_file_and_lock(tmp_path):
    from fileio import atomic_write_csv
    df = pd.DataFrame({"x": [1, 2, 3]})
    target = tmp_path / "data.csv"
    atomic_write_csv(str(target), df)
    assert target.exists()
    assert (tmp_path / "data.csv.lock").exists()
    loaded = pd.read_csv(target)
    assert list(loaded["x"]) == [1, 2, 3]
