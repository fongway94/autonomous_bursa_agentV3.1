def test_account_round_trip():
    from repository import load_account, save_account
    a = load_account()
    assert a["initial_capital"] == 20000.0
    save_account(cash_balance=12345.67)
    assert load_account()["cash_balance"] == 12345.67


def test_parameters_change_logged():
    from repository import load_parameters, save_parameters
    from logger import get_parameter_history
    save_parameters({**load_parameters(), "ema_fast": 7},
                    source="TEST", reason="unit test")
    hist = get_parameter_history(limit=5)
    assert any(h["source"] == "TEST" for h in hist)


def test_trade_insert_and_load():
    from repository import insert_trade, load_trades, get_trade
    tid = insert_trade({
        "ticker": "0166.KL", "name": "Inari", "sector": "Technology",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "entry_price": 3.0, "stop_loss": 2.85,
        "tp1": 3.15, "tp2": 3.30, "tp3": 3.45,
        "shares": 200, "lots": 2, "cost": 600.0,
        "fee": 0.9, "total_outlay": 600.9,
        "risk_per_share": 0.15, "actual_risk_pct": 5.0,
        "status": "ACTIVE", "phase": "FULL",
        "logged_at": "2026-05-27 09:00:00",
        "shares_remaining": 200,
        "entry_indicators": {"rsi": 55, "vol_ratio": 1.8,
                             "macd_hist": 0.01, "ema_trend_distance": 2.0},
    })
    assert tid > 0
    t = get_trade(tid)
    assert t["ticker"] == "0166.KL"
    assert t["entry_indicators"]["rsi"] == 55
    assert len(load_trades(status="ACTIVE")) == 1
