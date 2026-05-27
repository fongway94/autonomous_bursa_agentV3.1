def test_drawdown_levels():
    from risk_manager import check_drawdown_circuit_breaker
    r = check_drawdown_circuit_breaker(20000, 19500)  # 2.5 %
    assert r["allowed"] and r["level"] == "OK"

    r = check_drawdown_circuit_breaker(20000, 18000)  # 10 %
    assert r["allowed"] and r["level"] == "WARN_DRAWDOWN"

    r = check_drawdown_circuit_breaker(20000, 16000)  # 20 %
    assert not r["allowed"] and r["level"] == "STRICT_CIRCUIT_BREAKER"


def test_position_limits_max_concurrent():
    from risk_manager import check_position_limits
    fake_trades = [{"status": "ACTIVE"}] * 8  # at cap
    r = check_position_limits(fake_trades, 1000, "Technology", 20000)
    assert not r["allowed"]


def test_position_limits_sector_cap_reduces_size():
    from risk_manager import check_position_limits
    fake = [{"status": "ACTIVE", "sector": "Tech", "cost": 7000}]
    r = check_position_limits(fake, 3000, "Tech", 20000)  # sector cap 40 % = 8000
    # 7000 already, +3000 would be 10000 > 8000 → reduce
    assert r["allowed"]
    assert r["size_reduction_pct"] > 0


def test_full_risk_check_applies_size_multiplier():
    from risk_manager import run_full_risk_check
    fake = [{"status": "ACTIVE", "sector": "Tech", "cost": 7000}]
    r = run_full_risk_check(
        fake,
        {"ticker": "X", "sector": "Tech",
         "entry": 3.0, "stop_loss": 2.85,
         "cost": 3000, "risk_amount": 60},
        capital=20000, initial_capital=20000,
    )
    assert r["pass"] or r["size_multiplier"] < 1
    assert 0 <= r["size_multiplier"] <= 1
