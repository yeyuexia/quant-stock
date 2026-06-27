"""Tests for orders._effective_stop_pct — ATR-scaled core stop logic.

Extracted from test_orders.py to keep that file focused on state/safety
flows. The reconcile_to_targets ↔ stop-pct integration tests stay in
test_orders.py since they exercise reconcile, not the stop calc itself.
"""
import pandas as pd


def _ohlcv_constant(symbol: str, high: float, low: float, close: float, n: int = 30):
    """Build a MultiIndex OHLCV frame in the shape data.fetch_ohlcv returns."""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        ("High",  symbol): [high]  * n,
        ("Low",   symbol): [low]   * n,
        ("Close", symbol): [close] * n,
    }, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def test_effective_stop_pct_uses_atr_when_tighter(monkeypatch):
    """ATR pct between floor and base → returns ATR-scaled pct."""
    from quant.execution.orders import _effective_stop_pct
    # high=101, low=99 → TR≈2, last_close=100 → ATR/close=0.02 → 2*ATR/close=0.04
    # (above ATR_STOP_FLOOR_PCT 0.02, below STOP_LOSS_PCT 0.08)
    df = _ohlcv_constant("AAPL", high=101.0, low=99.0, close=100.0, n=30)
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - 0.04) < 1e-6


def test_effective_stop_pct_floors_low_vol(monkeypatch):
    """Near-zero-vol data → ATR pct below floor → clamped up to floor.

    Regression: BIL (T-bill ETF) produced a ~0.05% stop that fired on bid-ask
    noise. The ATR_STOP_FLOOR_PCT clamp now keeps it sane."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct
    # TR≈0.05 on a $91 close → 2*ATR/close ≈ 0.0011 (well below the 0.02 floor)
    df = _ohlcv_constant("LQD", high=91.025, low=90.975, close=91.0, n=30)
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("LQD", "core")
    assert abs(result - config.ATR_STOP_FLOOR_PCT) < 1e-9


def test_effective_stop_pct_defensive_symbol_skips_atr(monkeypatch):
    """Defensive / safe-haven symbols (BIL etc.) use the base stop, never the
    ATR scaling — and must not even call fetch_ohlcv."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct

    def _trap(*a, **kw):
        raise AssertionError("fetch_ohlcv must not be called for defensive symbols")
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", _trap)

    base = config.STOP_LOSS_PCT
    for sym in config.DEFENSIVE_SYMBOLS:
        assert _effective_stop_pct(sym, "core") == base


def test_effective_stop_pct_caps_at_base(monkeypatch):
    """High-vol data (ATR pct > base) → returns STOP_LOSS_PCT."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct
    # TR≈20 on a $100 close → 2*ATR/close = 0.40 (> any base)
    df = _ohlcv_constant("TSLA", high=110.0, low=90.0, close=100.0, n=30)
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("TSLA", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_aggressive_unchanged(monkeypatch):
    """Aggressive tranche short-circuits — does not call fetch_ohlcv."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct

    called = {"hit": False}
    def _trap(*a, **kw):
        called["hit"] = True
        raise AssertionError("fetch_ohlcv must not be called for aggressive")
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", _trap)

    result = _effective_stop_pct("TQQQ", "aggressive")
    assert result == config.AGGRESSIVE_PARAMS["stop_loss_pct"]
    assert called["hit"] is False


def test_effective_stop_pct_fallback_on_fetch_error(monkeypatch):
    """fetch_ohlcv raising → returns base, no exception escapes."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct

    def _boom(*a, **kw):
        raise RuntimeError("yfinance unavailable")
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", _boom)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_insufficient_data(monkeypatch):
    """Too few bars for ATR → returns base."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct
    df = _ohlcv_constant("AAPL", high=100.5, low=99.5, close=100.0, n=5)
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("AAPL", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9


def test_effective_stop_pct_fallback_on_zero_atr(monkeypatch):
    """Constant prices → ATR=0 → fallback to base (not 0)."""
    import quant.config as config
    from quant.execution.orders import _effective_stop_pct
    df = _ohlcv_constant("SHV", high=100.0, low=100.0, close=100.0, n=30)
    monkeypatch.setattr("quant.data.market.fetch_ohlcv", lambda tickers, period="1y": df)

    result = _effective_stop_pct("SHV", "core")
    assert abs(result - config.STOP_LOSS_PCT) < 1e-9
