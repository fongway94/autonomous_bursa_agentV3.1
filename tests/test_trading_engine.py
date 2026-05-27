"""
Cash-conservation property tests + lot-size enforcement.
"""

import pytest


def _market_regime():
    return {"regime_data": {"regime": "NEUTRAL"},
            "position_rules": {"conviction_pct": 50}}


def _analysis():
    return {"reasoning": "test", "rsi": 55, "vol_ratio": 1.5,
            "atr": 0.05, "support": 2.85, "resistance": 3.10,
            "macd_hist": 0.01, "ema_trend": 2.95}


def test_entry_below_lot_size_rejected():
    from trading_engine import execute_entry
    ok, tid, msg = execute_entry(
        "0166.KL", "Inari", "Technology",
        entry_price=3.0, stop_loss=2.85,
        tp1=3.15, tp2=3.30, tp3=3.45,
        signal_type="GOLD BUY (BREAKOUT)",
        shares=50,  # below 100-share lot
        analysis_data=_analysis(),
        market_regime=_market_regime(), confidence_score=70,
    )
    assert not ok
    assert "lot" in msg.lower()


def test_entry_rounds_down_to_lot():
    """137 shares should become 100."""
    from trading_engine import execute_entry
    from repository import get_trade

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 137,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    t = get_trade(tid)
    assert t["shares"] == 100
    assert t["lots"] == 1


def test_insufficient_cash_rejected():
    from trading_engine import execute_entry
    from repository import save_account

    save_account(cash_balance=10.0)
    ok, _, msg = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 1000,
        _analysis(), _market_regime(), 70,
    )
    assert not ok
    assert "insufficient" in msg.lower()


def test_cash_conservation_full_cycle_tp3():
    """
    Buy 100 sh @ 3.00, settle at TP3 = 3.45. Verify:
        final_cash == initial_cash + net_pnl
    where net_pnl includes both legs' fees + buy/sell slippage.
    """
    from trading_engine import execute_entry, execute_full_exit
    from repository import load_account, get_trade, save_account

    save_account(initial_capital=20000.0, cash_balance=20000.0,
                 total_equity=20000.0)
    pre = load_account()["cash_balance"]

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 100,
        _analysis(), _market_regime(), 70,
    )
    assert ok

    t = get_trade(tid)
    cash_after_entry = load_account()["cash_balance"]
    # entry cost ≈ filled_price * 100 * 1.0015
    assert cash_after_entry < pre

    ok, _ = execute_full_exit(tid, 3.45, reason="TP3 test",
                              outcome="WIN", actor="USER")
    assert ok
    post = load_account()["cash_balance"]
    t = get_trade(tid)

    # net_pnl from the trade
    realized = t["realized_pnl"]
    # ledger invariant
    assert abs((post - pre) - realized) < 0.05  # 2dp rounding tolerance


def test_partial_then_full_exit_preserves_cash():
    """
    Buy 200 sh, partial 100 at TP2, then full exit 100 at TP3.
    Verify final cash == initial - entry_outlay + partial_proceeds + full_proceeds.
    """
    from trading_engine import (execute_entry, execute_partial_exit,
                                execute_full_exit)
    from repository import load_account, get_trade

    pre = load_account()["cash_balance"]

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 200,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    after_entry = load_account()["cash_balance"]

    ok, _ = execute_partial_exit(tid, "TP2", 3.30, 100,
                                  reason="test partial")
    assert ok
    after_partial = load_account()["cash_balance"]
    assert after_partial > after_entry  # cash returned

    ok, _ = execute_full_exit(tid, 3.45, reason="rest",
                              outcome="WIN", actor="USER")
    assert ok
    final = load_account()["cash_balance"]
    t = get_trade(tid)
    # Ledger invariant — final cash equals starting cash + total realized P&L
    realized = t["realized_pnl"]
    assert abs((final - pre) - realized) < 0.15  # 2-leg 2dp rounding


def test_full_exit_loss_marked_correctly():
    from trading_engine import execute_entry, execute_full_exit
    from repository import get_trade

    ok, tid, _ = execute_entry(
        "0166.KL", "Inari", "Technology", 3.0, 2.85,
        3.15, 3.30, 3.45, "GOLD BUY (BREAKOUT)", 100,
        _analysis(), _market_regime(), 70,
    )
    assert ok
    execute_full_exit(tid, 2.85, reason="SL hit",
                      outcome="LOSS", actor="USER")
    t = get_trade(tid)
    assert t["status"] == "CLOSED"
    assert t["outcome"] == "LOSS"
    assert (t["realized_pnl"] or 0) < 0
