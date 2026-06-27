import quant.config as config


def test_agent_config_knobs_present():
    assert config.AGENT_DOSSIER_WORKERS == 12
    assert config.AGENT_INCLUDE_NEWS is True
    assert config.AGENT_SHORTLIST_PER_SOURCE == 4
    assert config.AGENT_SHORTLIST_N == 8
    assert config.AGENT_MAX_PICKS == 5
    assert config.AGENT_CONVICTION_FLOOR == 50
    assert config.AGENT_PEER_MIN_GROUP == 3
    assert config.AGENT_RSI_PERIOD == 14
    assert config.AGENT_REL_STRENGTH_LOOKBACK_DAYS == 63
    assert config.AGENT_BUY_BAND_ATR == 0.5
    assert config.AGENT_STOP_ATR_MULT == 1.5
    assert config.AGENT_TARGET_R == 2.5
