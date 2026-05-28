"""
Tests for the _explain_cycle_outcome helper that explains
WHY an auto-entry cycle ended with zero new positions.
"""

import pandas as pd


def _empty_summary():
    return {"scan_count": 0, "settled": 0, "partials": 0,
            "auto_entries": 0, "rejected": 0, "errors": []}


def _df(rows):
    return pd.DataFrame([
        {"ticker": t, "signal": sig, "confidence": c}
        for t, sig, c in rows
    ])


def _regime(name="BULL", threshold_pct=0.60):
    return {"regime_data": {"regime": name},
            "position_rules": {"new_signal_threshold": threshold_pct,
                                "max_concurrent_positions": 8,
                                "conviction_pct": 70}}


def test_explains_autotrade_disabled():
    from scheduler import _explain_cycle_outcome
    msg = _explain_cycle_outcome(
        _empty_summary(), _df([]), _regime(), 60.0,
        active_count=0, max_positions=8, autotrade_enabled=False,
    )
    assert "Auto-entry is OFF" in msg


def test_explains_empty_scan():
    from scheduler import _explain_cycle_outcome
    msg = _explain_cycle_outcome(
        _empty_summary(), _df([]), _regime(), 60.0,
        active_count=0, max_positions=8, autotrade_enabled=True,
    )
    assert "0 results" in msg or "0 scanned" in msg


def test_explains_no_gold_buys():
    from scheduler import _explain_cycle_outcome
    df = _df([
        ("0166.KL", "HOLD / WATCH", 50),
        ("5398.KL", "NEUTRAL", 40),
        ("1155.KL", "REDUCE / AVOID", 25),
    ])
    msg = _explain_cycle_outcome(
        _empty_summary(), df, _regime("BEAR", 0.80), 80.0,
        active_count=0, max_positions=3, autotrade_enabled=True,
    )
    assert "No GOLD BUY" in msg
    assert "BEAR" in msg


def test_explains_below_threshold_in_bear():
    """The exact case the user saw."""
    from scheduler import _explain_cycle_outcome
    df = _df([
        ("0166.KL", "GOLD BUY (BREAKOUT)", 65),
        ("5398.KL", "GOLD BUY (PULLBACK)", 72),
        ("1155.KL", "GOLD BUY (BREAKOUT)", 55),
    ])
    msg = _explain_cycle_outcome(
        _empty_summary(), df, _regime("BEAR", 0.80), 80.0,
        active_count=0, max_positions=3, autotrade_enabled=True,
    )
    assert "72" in msg
    assert "80" in msg
    assert "BEAR" in msg


def test_explains_at_position_cap():
    from scheduler import _explain_cycle_outcome
    df = _df([
        ("0166.KL", "GOLD BUY (BREAKOUT)", 85),
    ])
    msg = _explain_cycle_outcome(
        _empty_summary(), df, _regime("BULL", 0.60), 60.0,
        active_count=8, max_positions=8, autotrade_enabled=True,
    )
    assert "max concurrent" in msg
    assert "8/8" in msg


def test_explains_all_qualifiers_already_held():
    from scheduler import _explain_cycle_outcome
    from repository import insert_trade

    for tkr in ("0166.KL", "5398.KL"):
        insert_trade({
            "ticker": tkr, "name": "x", "sector": "Tech",
            "signal_type": "GOLD BUY (BREAKOUT)",
            "entry_price": 1.0, "stop_loss": 0.9,
            "tp1": 1.1, "tp2": 1.2, "tp3": 1.3,
            "shares": 100, "lots": 1, "cost": 100, "fee": 0.15,
            "total_outlay": 100.15, "risk_per_share": 0.1,
            "actual_risk_pct": 10, "status": "ACTIVE", "phase": "FULL",
            "logged_at": "2026-05-28 09:00:00",
            "shares_remaining": 100,
        })

    df = _df([
        ("0166.KL", "GOLD BUY (BREAKOUT)", 85),
        ("5398.KL", "GOLD BUY (PULLBACK)", 82),
    ])
    msg = _explain_cycle_outcome(
        _empty_summary(), df, _regime("BULL", 0.60), 60.0,
        active_count=2, max_positions=8, autotrade_enabled=True,
    )
    assert "already in active positions" in msg


def test_explains_risk_rejected():
    from scheduler import _explain_cycle_outcome
    summary = {**_empty_summary(), "rejected": 3}
    df = _df([
        ("0166.KL", "GOLD BUY (BREAKOUT)", 85),
        ("5398.KL", "GOLD BUY (PULLBACK)", 82),
        ("7113.KL", "GOLD BUY (BREAKOUT)", 81),
    ])
    msg = _explain_cycle_outcome(
        summary, df, _regime("BULL", 0.60), 60.0,
        active_count=0, max_positions=8, autotrade_enabled=True,
    )
    assert "rejected by risk" in msg
    assert "3" in msg
