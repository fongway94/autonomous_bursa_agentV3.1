"""
Tests for v3.1.4 regime-history snapshot + trend computation.

The cycle-explanation message now includes regime trend so the user
can tell "BEAR is weakening, entries may resume" vs "BEAR is deepening,
stay defensive longer".
"""

import pandas as pd
from datetime import datetime, timezone, timedelta


def test_record_and_read_regime_snapshot():
    """A single recorded snapshot is readable."""
    from repository import record_regime_snapshot, get_regime_trend
    record_regime_snapshot("BEAR", 50.0, trend_score=25.0,
                            ema_200_vs_price=-3.2, klci_rsi=38.0)
    t = get_regime_trend(lookback_hours=24)
    assert t["samples"] == 1
    assert t["current_conviction"] == 50.0
    assert t["ema_200_distance_pct"] == -3.2


def test_regime_trend_detects_weakening_bear():
    """Multiple snapshots with falling BEAR conviction → WEAKENING."""
    from repository import record_regime_snapshot, get_regime_trend

    # Conviction was high in past, now lower
    for conv in [70.0, 65.0, 60.0, 55.0, 50.0]:
        record_regime_snapshot("BEAR", conv,
                                ema_200_vs_price=-2.0)

    # Final value 40 — much lower than 60 avg
    record_regime_snapshot("BEAR", 40.0, ema_200_vs_price=-1.0)

    t = get_regime_trend(lookback_hours=24)
    assert t["samples"] == 6
    assert t["current_conviction"] == 40.0
    assert t["change"] < 0
    assert t["direction"] == "WEAKENING"


def test_regime_trend_detects_strengthening_bear():
    """Rising BEAR conviction → STRENGTHENING."""
    from repository import record_regime_snapshot, get_regime_trend
    for conv in [40.0, 45.0, 48.0]:
        record_regime_snapshot("BEAR", conv, ema_200_vs_price=-1.0)
    record_regime_snapshot("BEAR", 65.0, ema_200_vs_price=-2.5)

    t = get_regime_trend(lookback_hours=24)
    assert t["direction"] == "STRENGTHENING"
    assert t["change"] > 3


def test_regime_trend_stable_when_minimal_change():
    from repository import record_regime_snapshot, get_regime_trend
    for conv in [50.0, 51.0, 49.0, 50.0, 51.0]:
        record_regime_snapshot("NEUTRAL", conv)
    t = get_regime_trend(lookback_hours=24)
    assert t["direction"] == "STABLE"


def test_regime_trend_empty_when_no_data():
    from repository import get_regime_trend
    t = get_regime_trend(lookback_hours=24)
    assert t["samples"] == 0
    assert t["direction"] == "UNKNOWN"


def test_cycle_explanation_includes_trend_in_bear():
    """
    The full scheduler explanation should mention regime conviction
    trend + KLCI distance from 200-EMA when in BEAR + below-threshold.
    """
    from repository import record_regime_snapshot
    from scheduler import _explain_cycle_outcome

    # Seed some BEAR snapshots — weakening
    for conv in [70.0, 65.0, 55.0]:
        record_regime_snapshot("BEAR", conv, ema_200_vs_price=-2.5)
    record_regime_snapshot("BEAR", 45.0, ema_200_vs_price=-1.8)

    df = pd.DataFrame([
        {"ticker": "0166.KL", "signal": "GOLD BUY (BREAKOUT)",
         "confidence": 44},
        {"ticker": "5398.KL", "signal": "GOLD BUY (PULLBACK)",
         "confidence": 38},
    ])
    regime = {"regime_data": {"regime": "BEAR"},
              "position_rules": {"new_signal_threshold": 0.80,
                                  "max_concurrent_positions": 3}}

    msg = _explain_cycle_outcome(
        {"scan_count": 80, "settled": 0, "partials": 0,
         "auto_entries": 0, "rejected": 0, "errors": []},
        df, regime, 80.0,
        active_count=0, max_positions=3,
        autotrade_enabled=True,
    )

    # Should include the bad news (below threshold)
    assert "below the BEAR" in msg
    assert "44" in msg
    # Should include the good news (trend weakening)
    assert "conviction" in msg.lower()
    assert "weakening" in msg.lower()
    # Should include the actionable signal
    assert "200-EMA" in msg


def test_cycle_explanation_handles_strengthening_bear():
    """If BEAR is strengthening, message should NOT say 'may resume soon'."""
    from repository import record_regime_snapshot
    from scheduler import _explain_cycle_outcome

    for conv in [40.0, 45.0]:
        record_regime_snapshot("BEAR", conv, ema_200_vs_price=-1.0)
    record_regime_snapshot("BEAR", 70.0, ema_200_vs_price=-3.0)

    df = pd.DataFrame([
        {"ticker": "0166.KL", "signal": "GOLD BUY (BREAKOUT)",
         "confidence": 30},
    ])
    regime = {"regime_data": {"regime": "BEAR"},
              "position_rules": {"new_signal_threshold": 0.80,
                                  "max_concurrent_positions": 3}}

    msg = _explain_cycle_outcome(
        {"scan_count": 80, "settled": 0, "partials": 0,
         "auto_entries": 0, "rejected": 0, "errors": []},
        df, regime, 80.0,
        active_count=0, max_positions=3,
        autotrade_enabled=True,
    )

    assert "strengthening" in msg.lower() or "deeper" in msg.lower()
    assert "may resume soon" not in msg.lower()
