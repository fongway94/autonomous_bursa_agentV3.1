def test_trade_log_write_and_read():
    from logger import log_trade_event, get_trade_log
    log_trade_event("ENTRY_EXECUTED", trade_id=999, ticker="TEST.KL",
                    actor="USER", payload={"price": 1.0, "shares": 100})
    rows = get_trade_log(limit=5, event_filter="ENTRY_EXECUTED")
    assert any(r["trade_id"] == 999 for r in rows)


def test_scheduler_log_write_and_read():
    from logger import log_scheduler_event, get_scheduler_log
    log_scheduler_event("HEARTBEAT", "test heartbeat")
    log_scheduler_event("ERROR", "boom", "ERROR")
    rows = get_scheduler_log(limit=10)
    assert any(r["event"] == "HEARTBEAT" for r in rows)
    err_rows = get_scheduler_log(limit=10, level="ERROR")
    assert any(r["event"] == "ERROR" for r in err_rows)


def test_learning_event_write():
    from logger import log_learning_event, get_learning_events
    log_learning_event("BAYES_UPDATE", "state#1 BUY → WIN",
                       changes={"alpha": 2.0}, metrics={"r": 1.5})
    ev = get_learning_events(limit=5)
    assert any(e["event_type"] == "BAYES_UPDATE" for e in ev)


def test_parameter_change_logged():
    from repository import save_parameters, load_parameters
    from logger import get_parameter_history
    p = load_parameters()
    save_parameters({**p, "ema_fast": 5}, source="TEST", reason="ut")
    hist = get_parameter_history(limit=5)
    assert any(h["source"] == "TEST" for h in hist)


def test_bias_change_logged():
    from logger import log_bias_change, get_bias_history
    log_bias_change("breakout_bias", 1.0, 1.05, trade_id=1, outcome="WIN")
    h = get_bias_history(limit=5)
    assert any(r["field"] == "breakout_bias" for r in h)
