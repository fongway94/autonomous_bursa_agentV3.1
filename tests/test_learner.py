def test_discretize_state_stable():
    from learner import discretize_state
    s1 = discretize_state(55, 1.5, 1.2, 0.05)
    s2 = discretize_state(55, 1.5, 1.2, 0.05)
    assert s1 == s2
    assert 0 <= s1 < 250


def test_bayesian_update_increases_alpha_on_win():
    from learner import learn_from_trade_outcome
    from db import connect

    fake_trade = {
        "id": 1, "ticker": "0166.KL", "sector": "Technology",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "outcome": "WIN", "closed_pnl": 100.0, "cost": 1000.0,
        "risk_per_share": 0.15, "shares": 100,
        "market_regime": "NEUTRAL",
        "entry_indicators": {"rsi": 55, "vol_ratio": 1.5,
                             "ema_trend_distance": 1.0, "macd_hist": 0.05},
    }
    res = learn_from_trade_outcome(fake_trade)
    assert res["alpha"] > 1.0
    # Apply same trade again — alpha should grow further
    res2 = learn_from_trade_outcome(fake_trade)
    assert res2["alpha"] > res["alpha"]


def test_bayesian_update_increases_beta_on_loss():
    from learner import learn_from_trade_outcome
    t = {
        "id": 2, "ticker": "0166.KL", "sector": "Technology",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "outcome": "LOSS", "closed_pnl": -100.0, "cost": 1000.0,
        "risk_per_share": 0.15, "shares": 100,
        "market_regime": "NEUTRAL",
        "entry_indicators": {"rsi": 55, "vol_ratio": 1.5,
                             "ema_trend_distance": 1.0, "macd_hist": 0.05},
    }
    res = learn_from_trade_outcome(t)
    assert res["beta"] > 1.0


def test_compute_state_action_score_returns_action():
    from learner import compute_state_action_score
    out = compute_state_action_score(50, 70, "NEUTRAL", 0.0)
    assert out["action"] in ("BUY", "HOLD", "AVOID")
    assert 0 < out["confidence_modifier"] <= 1.3
    assert "reasoning" in out
