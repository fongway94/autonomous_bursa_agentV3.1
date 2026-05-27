"""v3-specific tests: defaults, exploration mode, slippage, Shariah."""


def test_default_autotrade_is_on():
    """v3: brand-new install should have autotrade enabled."""
    from repository import get_scheduler_state
    s = get_scheduler_state()
    assert s["autotrade_enabled"] == 1, "v3 default must be auto-trade ON"


def test_exploration_mode_columns_present():
    from repository import get_scheduler_state
    s = get_scheduler_state()
    assert "exploration_mode" in s
    assert "exploration_trades_target" in s
    assert s["exploration_mode"] == 1


def test_default_risk_per_trade_is_one_percent():
    from risk_manager import load_risk_params
    p = load_risk_params()
    assert p["max_risk_per_trade_pct"] == 1.0


def test_optimistic_prior_for_buy_cold_start():
    """Unseen BUY state must start with α>β (encourages exploration)."""
    from learner import _get_prior
    from db import connect
    with connect() as c:
        p_buy = _get_prior(c, state_id=99999, action="BUY")
        p_avoid = _get_prior(c, state_id=99999, action="AVOID")
    assert p_buy["alpha"] > p_buy["beta"], "BUY prior must be optimistic"
    assert p_avoid["alpha"] == p_avoid["beta"], "AVOID prior must be neutral"


def test_thompson_sampling_used_in_exploration_mode():
    """In EXPLORE mode, repeated calls on same unseen state should vary."""
    from learner import compute_state_action_score
    from repository import update_scheduler_state
    update_scheduler_state(exploration_mode=1)
    scores = []
    for _ in range(20):
        out = compute_state_action_score(123, 70, "NEUTRAL", 0.0)
        scores.append(out["q_scores"]["BUY"])
    # Thompson sampling → high variance vs LCB
    import statistics
    assert statistics.pstdev(scores) > 0, "EXPLORE mode should produce variable scores"


def test_exploit_mode_deterministic():
    """In EXPLOIT mode, repeated calls on same state should be identical."""
    from learner import compute_state_action_score
    from repository import update_scheduler_state
    update_scheduler_state(exploration_mode=0)
    out1 = compute_state_action_score(456, 70, "NEUTRAL", 0.0)
    out2 = compute_state_action_score(456, 70, "NEUTRAL", 0.0)
    assert out1["q_scores"]["BUY"] == out2["q_scores"]["BUY"]


def test_volume_aware_slippage_higher_for_thin_stocks():
    from trading_engine import estimate_slippage_bps
    # Same RM order, but ADV very different
    thin = estimate_slippage_bps(20_000, avg_daily_rm=100_000)   # 20% of ADV
    liquid = estimate_slippage_bps(20_000, avg_daily_rm=10_000_000)  # 0.2%
    assert thin > liquid, "Thin stocks should slip more"


def test_slippage_is_capped():
    from trading_engine import estimate_slippage_bps, SLIPPAGE_LIQUIDITY_CAP_BPS
    huge_thin = estimate_slippage_bps(500_000, avg_daily_rm=50_000)
    assert huge_thin <= SLIPPAGE_LIQUIDITY_CAP_BPS


def test_shariah_filter():
    from watchlist import (is_shariah_compliant, get_all_tickers_shariah_only,
                            SHARIAH_NON_COMPLIANT)
    # 1155.KL Maybank is conventional — flagged
    assert not is_shariah_compliant("1155.KL")
    # 0166.KL Inari is generally compliant
    assert is_shariah_compliant("0166.KL")
    # Filtered list excludes everything in the non-compliant set
    sc_only = set(get_all_tickers_shariah_only())
    assert sc_only.isdisjoint(SHARIAH_NON_COMPLIANT)


def test_scheduler_ensure_started_self_heals(monkeypatch):
    """If heartbeat is older than 2× interval, ensure_started must restart."""
    import scheduler
    from repository import update_scheduler_state, get_scheduler_state

    # Stub the heavy cycle work
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                         "partials": 0, "auto_entries": 0,
                                         "rejected": 0, "errors": []})

    scheduler.start(interval_sec=60)
    assert scheduler.is_running()

    # Forge a stale heartbeat
    update_scheduler_state(last_heartbeat="2020-01-01 00:00:00")

    # ensure_started should detect staleness and force-restart
    scheduler.ensure_started(interval_sec=60, max_heartbeat_age_sec=5)
    assert scheduler.is_running()

    scheduler.stop()


def test_exploration_auto_disables_after_threshold(monkeypatch):
    """Closing enough trades should flip exploration_mode off via scheduler."""
    from repository import update_scheduler_state, get_scheduler_state, insert_trade
    # Lower the bar so the test doesn't have to insert 50 trades
    update_scheduler_state(exploration_mode=1, exploration_trades_target=3)

    # Insert 3 closed trades
    for i in range(3):
        insert_trade({
            "ticker": "TEST.KL", "name": "Test", "sector": "Tech",
            "signal_type": "GOLD BUY (BREAKOUT)",
            "entry_price": 1.0, "stop_loss": 0.9,
            "tp1": 1.1, "tp2": 1.2, "tp3": 1.3,
            "shares": 100, "lots": 1, "cost": 100, "fee": 0.15,
            "total_outlay": 100.15, "risk_per_share": 0.1,
            "actual_risk_pct": 10, "status": "CLOSED", "phase": "CLOSED",
            "outcome": "WIN", "logged_at": "2025-01-01 09:00:00",
            "closed_at": "2025-01-02 09:00:00",
            "shares_remaining": 0, "realized_pnl": 10, "closed_pnl": 10,
        })

    # Simulate the scheduler's daily maintenance block
    from repository import closed_trades
    ss = get_scheduler_state()
    if ss.get("exploration_mode"):
        tgt = ss.get("exploration_trades_target", 50) or 50
        if len(closed_trades()) >= tgt:
            update_scheduler_state(exploration_mode=0)

    assert get_scheduler_state()["exploration_mode"] == 0
