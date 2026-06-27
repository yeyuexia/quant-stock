# tests/test_signal_rank.py
def test_momentum_signals_expose_rank_per_holding():
    from quant.signals.momentum import generate_signals
    sig = generate_signals()
    # Each (ticker, weight, rank) triple; rank 1 = best, ascending.
    assert "holdings_ranked" in sig
    for ticker, weight, rank in sig["holdings_ranked"]:
        assert isinstance(rank, int)
        assert rank >= 1


def test_momentum_top_1_is_rank_1():
    from quant.signals.momentum import generate_signals
    sig = generate_signals()
    if sig["holdings_ranked"]:
        top = sig["holdings_ranked"][0]
        assert top[2] == 1  # rank


def test_screener_output_has_rank_column():
    from quant.signals.screener import screen_stocks
    df = screen_stocks(tickers=["AAPL", "MSFT", "GOOGL"])
    if df is None or df.empty:
        return
    assert "rank" in df.columns
    assert df.iloc[0]["rank"] == 1
