import config


def test_new_flags_have_expected_defaults():
    assert config.ADOPT_EXTERNAL_POSITIONS is True
    assert config.UNKNOWN_MV_HALT_PCT == 0.20
    assert config.ENFORCE_STOPS is True
