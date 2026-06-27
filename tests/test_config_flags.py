import quant.config as config


def test_new_flags_have_expected_defaults():
    assert config.ADOPT_EXTERNAL_POSITIONS is True
    assert config.UNKNOWN_MV_HALT_PCT == 0.20
    assert config.ENFORCE_STOPS is True


def test_ensemble_config_defaults():
    import quant.config as config
    assert config.VS_MIN_DOLLAR_VOLUME == 5_000_000
    assert config.VS_MIN_PRICE == 5.0
    assert config.VS_MIN_MARKET_CAP == 300_000_000
    assert config.VS_TOP_N == 20
    assert config.VS_PREFILTER_MAX == 500
    assert config.VS_FETCH_WORKERS == 12
    assert config.ENSEMBLE_TOP_N == 4
    assert config.ENSEMBLE_STRATEGIES == ["value", "canslim"]
    assert config.ENSEMBLE_STRATEGY_TIMEOUT_SEC == 240
    assert config.VS_TRACK_A["peg_max"] == 1.0 and config.VS_TRACK_A["pe_max"] == 20.0
    assert config.VS_TRACK_B["ps_max"] == 6.0 and config.VS_TRACK_B["cash_runway_quarters_min"] == 6
    assert not hasattr(config, "VS_WEIGHTS")
