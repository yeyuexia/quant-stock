import quant.strategies.value.prefilter as value_prefilter


def test_prefilter_drops_cheap_and_illiquid(monkeypatch):
    import quant.config as config
    monkeypatch.setattr(config, "VS_MIN_PRICE", 5.0)
    monkeypatch.setattr(config, "VS_MIN_DOLLAR_VOLUME", 5_000_000)
    data = {
        "OK":    (50.0, 9_000_000),   # passes
        "CHEAP": (3.0, 9_000_000),    # below price floor
        "THIN":  (50.0, 1_000_000),   # below $-vol gate
        "OK2":   (20.0, 6_000_000),   # passes
    }
    out = value_prefilter.prefilter(list(data), price_fn=lambda ts: data)
    assert set(out) == {"OK", "OK2"}
    assert out[0] == "OK"             # higher dollar-volume first


def test_prefilter_caps_at_max_keep():
    data = {f"T{i}": (10.0, (100-i)*1e6) for i in range(10)}
    out = value_prefilter.prefilter(list(data), price_fn=lambda ts: data, max_keep=3)
    assert out == ["T0", "T1", "T2"]   # top-3 by dollar-volume
