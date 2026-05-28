# tests/conftest.py
"""
Pytest config:

* Redirect DATA_DIR to a temp directory for every test session.
* Re-import all DB-touching modules so they see the new path.
"""

import os
import sys
import tempfile
import importlib

import pytest

# Resolve project root (one level up from tests/)
_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _PROJ)


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_dir():
    tmp = tempfile.mkdtemp(prefix="bursa_test_")
    os.environ["HOME"] = tmp  # makes DATA_DIR resolve to <tmp>/.bursa_agent_data

    # (Re)import in order — persistence + app need fresh DATA_DIR too
    for mod_name in [
        "db", "logger", "data_quality", "repository",
        "risk_manager", "trading_engine", "learner",
        "market_analyzer", "scheduler", "watchlist", "evaluation",
        "persistence", "notifier", "live_trigger",
        "broker_adapter", "maintenance_reminders", "app",
    ]:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)
    yield tmp


@pytest.fixture(autouse=True)
def _reset_db_between_tests():
    """Truncate all volatile tables AND reset singletons before each test."""
    from db import connect
    with connect() as c:
        for tbl in ("trades", "partial_exits", "trade_log",
                    "scheduler_log", "learning_events", "parameter_history",
                    "bias_history", "state_priors", "data_quality_log",
                    "scan_cache", "alert_log", "maintenance_state",
                    "regime_history", "meta", "custom_watchlist"):
            c.execute(f"DELETE FROM {tbl}")
        # Reset scheduler_state singleton to v3 defaults
        c.execute(
            "UPDATE scheduler_state SET "
            "running=0, interval_sec=3600, last_run_at=NULL, "
            "next_run_at=NULL, last_heartbeat=NULL, "
            "consecutive_failures=0, last_error=NULL, "
            "autotrade_enabled=1, autoexit_enabled=1, kill_switch=0, "
            "exploration_mode=1, exploration_trades_target=50, "
            "owner_pid=0 "
            "WHERE id=1"
        )
        # Reset live_trigger_config singleton
        c.execute(
            "UPDATE live_trigger_config SET "
            "enabled=0, min_confidence=70.0, exploit_mode_only=0, "
            "alert_on_entry=1, alert_on_full_exit=1, alert_on_stop_loss=1, "
            "alert_on_trailing_stop=1, alert_on_partial_exit=0, "
            "alert_on_risk_rejected=0, telegram_enabled=1, "
            "email_enabled=0, email_recipients='', actor_filter='AGENT' "
            "WHERE id=1"
        )
    # Reset singletons
    from repository import save_account, save_bias_state, save_parameters
    save_account(initial_capital=20000.0, cash_balance=20000.0,
                 total_equity=20000.0)
    save_bias_state({"breakout_bias": 1.0, "pullback_bias": 1.0,
                     "sector_biases": {}, "strategy_stats": {},
                     "sector_stats": {}, "total_closed_trades": 0,
                     "system_win_rate": 0.5})
    save_parameters({
        "ema_trend": 200, "ema_fast": 10, "ema_slow": 20,
        "rsi_oversold_pullback": 40.0, "rsi_overbought": 70.0,
        "volume_surge_ratio": 1.5, "breakout_period": 20,
        "atr_period": 14, "atr_multiplier_stop": 1.5,
        "min_price": 0.30, "max_price": 4.00,
    }, source="TEST", reason="reset")
