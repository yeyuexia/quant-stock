import config


def test_new_flags_have_expected_defaults():
    assert config.ADOPT_EXTERNAL_POSITIONS is True
    assert config.UNKNOWN_MV_HALT_PCT == 0.20
    assert config.ENFORCE_STOPS is True


def test_ensemble_config_defaults():
    import config
    assert config.VS_MIN_DOLLAR_VOLUME == 2_000_000
    assert config.VS_MIN_PRICE == 5.0
    assert config.VS_MIN_MARKET_CAP == 300_000_000
    assert config.VS_TOP_N == 20
    assert config.VS_WEIGHTS == {"value": 0.5, "quality": 0.35, "improving": 0.15}
    assert config.ENSEMBLE_TOP_N == 4
    assert config.ENSEMBLE_STRATEGIES == ["value", "canslim"]
