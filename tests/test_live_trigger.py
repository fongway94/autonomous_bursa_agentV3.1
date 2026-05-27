"""Live trigger filter, dedup, broker adapter, and dispatch tests."""

import pytest


# ----- Config IO -----

def test_default_config_disabled():
    from live_trigger import load_config
    cfg = load_config()
    assert cfg["enabled"] == 0
    assert cfg["min_confidence"] == 70.0
    assert cfg["alert_on_entry"] == 1
    assert cfg["actor_filter"] == "AGENT"


def test_save_and_reload_config():
    from live_trigger import save_config, load_config
    save_config({"enabled": True, "min_confidence": 80.0,
                 "exploit_mode_only": True,
                 "telegram_enabled": True, "email_enabled": False,
                 "email_recipients": "a@b.com, c@d.com",
                 "actor_filter": "BOTH"})
    cfg = load_config()
    assert cfg["enabled"] == 1
    assert cfg["min_confidence"] == 80.0
    assert cfg["exploit_mode_only"] == 1
    assert "a@b.com" in cfg["email_recipients"]
    assert cfg["actor_filter"] == "BOTH"


# ----- Filter logic -----

def test_filter_blocks_when_master_off():
    from live_trigger import _should_fire, save_config
    save_config({"enabled": False})
    ok, reason = _should_fire("ENTRY", {}, "AGENT", {"enabled": 0})
    assert not ok
    assert reason == "live_trigger_disabled"


def test_filter_blocks_below_confidence():
    from live_trigger import _should_fire
    cfg = {"enabled": 1, "min_confidence": 75.0, "exploit_mode_only": 0,
           "actor_filter": "AGENT", "alert_on_entry": 1}
    ok, _ = _should_fire("ENTRY",
                         {"confidence_score": 60.0},
                         "AGENT", cfg)
    assert not ok


def test_filter_passes_above_confidence():
    from live_trigger import _should_fire
    cfg = {"enabled": 1, "min_confidence": 75.0, "exploit_mode_only": 0,
           "actor_filter": "AGENT", "alert_on_entry": 1}
    ok, _ = _should_fire("ENTRY",
                         {"confidence_score": 85.0},
                         "AGENT", cfg)
    assert ok


def test_filter_blocks_manual_when_agent_only():
    from live_trigger import _should_fire
    cfg = {"enabled": 1, "min_confidence": 50.0, "exploit_mode_only": 0,
           "actor_filter": "AGENT", "alert_on_entry": 1}
    ok, reason = _should_fire("ENTRY",
                              {"confidence_score": 90.0},
                              "USER", cfg)
    assert not ok
    assert reason == "actor_not_agent"


def test_filter_allows_manual_when_both():
    from live_trigger import _should_fire
    cfg = {"enabled": 1, "min_confidence": 50.0, "exploit_mode_only": 0,
           "actor_filter": "BOTH", "alert_on_entry": 1}
    ok, _ = _should_fire("ENTRY",
                         {"confidence_score": 90.0},
                         "USER", cfg)
    assert ok


def test_filter_event_type_disabled():
    from live_trigger import _should_fire
    cfg = {"enabled": 1, "min_confidence": 50.0, "exploit_mode_only": 0,
           "actor_filter": "AGENT", "alert_on_stop_loss": 0}
    ok, reason = _should_fire("STOP_LOSS", {}, "AGENT", cfg)
    assert not ok
    assert "STOP_LOSS_disabled" in reason


def test_filter_exploit_only_blocks_in_explore_mode():
    from live_trigger import _should_fire
    from repository import update_scheduler_state
    update_scheduler_state(exploration_mode=1)
    cfg = {"enabled": 1, "min_confidence": 50.0, "exploit_mode_only": 1,
           "actor_filter": "AGENT", "alert_on_entry": 1}
    ok, reason = _should_fire("ENTRY",
                              {"confidence_score": 95.0},
                              "AGENT", cfg)
    assert not ok
    assert reason == "still_in_explore_mode"


def test_filter_exploit_only_allows_in_exploit_mode():
    from live_trigger import _should_fire
    from repository import update_scheduler_state
    update_scheduler_state(exploration_mode=0)
    cfg = {"enabled": 1, "min_confidence": 50.0, "exploit_mode_only": 1,
           "actor_filter": "AGENT", "alert_on_entry": 1}
    ok, _ = _should_fire("ENTRY",
                         {"confidence_score": 95.0},
                         "AGENT", cfg)
    assert ok


# ----- Dedup -----

def test_dedup_per_trade_event(monkeypatch):
    from live_trigger import fire, save_config, _was_already_sent
    from repository import insert_trade

    save_config({"enabled": True, "telegram_enabled": False,
                 "email_enabled": False, "min_confidence": 0,
                 "actor_filter": "BOTH"})

    tid = insert_trade({
        "ticker": "TEST.KL", "name": "Test", "sector": "Tech",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "entry_price": 1.0, "stop_loss": 0.9,
        "tp1": 1.1, "tp2": 1.2, "tp3": 1.3,
        "shares": 100, "lots": 1, "cost": 100.0, "fee": 0.15,
        "total_outlay": 100.15, "risk_per_share": 0.1,
        "actual_risk_pct": 10, "status": "ACTIVE", "phase": "FULL",
        "logged_at": "2026-01-01 09:00:00",
        "shares_remaining": 100,
        "confidence_score": 80,
    })

    # First fire - should succeed
    res1 = fire("ENTRY", trade_id=tid, ticker="TEST.KL", actor="AGENT")
    assert res1 is not None  # not skipped

    # Second fire same event same trade - dedup
    assert _was_already_sent(tid, "ENTRY")
    res2 = fire("ENTRY", trade_id=tid, ticker="TEST.KL", actor="AGENT")
    assert res2 is None  # deduped


# ----- Broker adapter contract -----

def test_noop_adapter_implements_interface():
    from broker_adapter import get_broker_adapter
    a = get_broker_adapter("NOOP")
    assert a.connect() is True
    assert a.is_connected() is True
    assert a.get_cash_balance() == 0.0
    assert a.list_positions() == []


def test_moomoo_adapter_raises_not_implemented():
    from broker_adapter import get_broker_adapter
    a = get_broker_adapter("MOOMOO")
    with pytest.raises(NotImplementedError):
        a.connect()
    with pytest.raises(NotImplementedError):
        a.get_cash_balance()


# ----- Format integration -----

def test_format_entry_message():
    from live_trigger import _format_entry
    trade = {
        "ticker": "0166.KL", "name": "Inari",
        "logged_at": "2026-06-15 10:00:00",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "confidence_score": 82.0, "shares": 1000,
        "entry_price": 3.0, "stop_loss": 2.85, "tp1": 3.225,
        "tp2": 3.30, "tp3": 3.45,
        "sector": "Technology", "market_regime": "BULL",
        "actual_risk_pct": 5.0,
        "entry_reasoning": "Strong breakout with volume.",
    }
    text, html, subj = _format_entry(trade, {"exploration_mode": 0})
    assert "0166.KL" in text
    assert "BUY 1,000 shares @ RM 3.000" in text
    assert "EXPLOIT" in text
    assert "[BursaAI] ENTRY" in subj


# ----- Notifier safety -----

def test_telegram_returns_error_without_creds(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from notifier import send_telegram
    ok, err = send_telegram("test")
    assert not ok
    assert "TELEGRAM" in err


def test_email_returns_error_without_creds(monkeypatch):
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)
    monkeypatch.delenv("ALERT_SMTP_USER", raising=False)
    monkeypatch.delenv("ALERT_SMTP_PASSWORD", raising=False)
    from notifier import send_email
    ok, err = send_email("s", "b", ["a@b.com"])
    assert not ok
    assert "SMTP" in err.upper() or "smtp" in err
