from quant.schema import ProposedChange


def _change(**kwargs):
    defaults = dict(
        key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075,
        rationale="r", detailed_plan="p", expected_effect="e",
        risk_tier="low", confidence=0.7,
    )
    defaults.update(kwargs)
    return ProposedChange(**defaults)


def test_classify_stop_loss_within_band_is_low():
    from quant.applier import classify_change
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    assert classify_change(c) == "low"


def test_classify_stop_loss_out_of_band_is_high():
    from quant.applier import classify_change
    # +30% from 0.08 is 0.104, outside the ±20% band → high-risk
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.104)
    assert classify_change(c) == "high"


def test_classify_stop_loss_out_of_absolute_bounds_is_rejected():
    from quant.applier import classify_change
    # 0.50 is outside the absolute bound [0.04, 0.20]
    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.50)
    assert classify_change(c) == "rejected_out_of_bounds"


def test_classify_momentum_top_n_is_always_high():
    from quant.applier import classify_change
    c = _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3)
    assert classify_change(c) == "high"


def test_classify_daily_max_orders_is_forbidden():
    from quant.applier import classify_change
    c = _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100)
    assert classify_change(c) == "forbidden"


def test_classify_watchlist_addition_is_low():
    from quant.applier import classify_change
    current = ["SPY", "QQQ"]
    proposed = current + ["PLTR"]
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "low"


def test_classify_watchlist_removal_is_high():
    from quant.applier import classify_change
    current = ["SPY", "QQQ", "IWM"]
    proposed = ["SPY", "QQQ"]
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "high"


def test_classify_watchlist_over_size_cap_is_rejected():
    from quant.applier import classify_change
    current = [f"T{i}" for i in range(99)]
    proposed = current + ["NEW_ONE", "NEW_TWO"]    # >100
    c = _change(key="WATCHLIST", current_value=current, proposed_value=proposed)
    assert classify_change(c) == "rejected_out_of_bounds"


def test_apply_writes_low_risk_to_overrides(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    proposals_path = tmp_path / "proposals.json"
    tg_path = tmp_path / "tg.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(proposals_path))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tg_path))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    result = applier.apply([c])

    assert len(result.applied_low) == 1
    import json as _j
    overrides = _j.loads(overrides_path.read_text())
    assert overrides["STOP_LOSS_PCT"] == 0.075


def test_apply_writes_high_risk_to_proposals(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    proposals_path = tmp_path / "proposals.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(proposals_path))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3)
    result = applier.apply([c])

    assert len(result.queued_high) == 1
    import json as _j
    queue = _j.loads(proposals_path.read_text())
    assert queue[0]["key"] == "MOMENTUM_TOP_N"
    assert "id" in queue[0]
    assert "expires_at" in queue[0]


def test_apply_rejects_forbidden_and_records(tmp_path, monkeypatch):
    import quant.applier as applier
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(tmp_path / "o.json"))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    c = _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100)
    result = applier.apply([c])

    assert len(result.rejected_forbidden) == 1
    # No low-risk changes → overrides file not written
    assert not (tmp_path / "o.json").exists()


def test_apply_dry_run_writes_dry_artifact_only(tmp_path, monkeypatch):
    import quant.applier as applier
    overrides_path = tmp_path / "overrides.json"
    dry_path = tmp_path / "dry.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(overrides_path))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tmp_path / "tg.json"))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))
    monkeypatch.setattr(applier, "DRY_RUN_PATH", str(dry_path))

    c = _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075)
    result = applier.apply([c], dry_run=True)

    assert len(result.applied_low) == 1
    # In dry-run: overrides.json NOT written; dry artifact IS.
    assert not overrides_path.exists()
    assert dry_path.exists()


def test_tg_notification_contains_all_sections(tmp_path, monkeypatch):
    import quant.applier as applier
    tg_path = tmp_path / "tg.json"
    monkeypatch.setattr(applier, "OVERRIDES_PATH", str(tmp_path / "o.json"))
    monkeypatch.setattr(applier, "PROPOSALS_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(applier, "TG_NOTIFY_PATH", str(tg_path))
    monkeypatch.setattr(applier, "AUDIT_LOG_PATH", str(tmp_path / "audit.log"))

    changes = [
        _change(key="STOP_LOSS_PCT", current_value=0.08, proposed_value=0.075),
        _change(key="MOMENTUM_TOP_N", current_value=4, proposed_value=3),
        _change(key="DAILY_MAX_ORDERS", current_value=40, proposed_value=100),
    ]
    applier.apply(changes)
    import json as _j
    notifs = _j.loads(tg_path.read_text())
    assert len(notifs) >= 1
    latest = notifs[-1]
    assert "message" in latest
    msg = latest["message"]
    assert "AUTO-APPLIED" in msg
    assert "NEEDS YOUR APPROVAL" in msg
    assert "REJECTED" in msg
