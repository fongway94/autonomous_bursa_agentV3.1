# tests/conftest.py
"""
Pytest config:

* Redirect DATA_DIR to a temp directory for every test session.
* Re-import all DB-touching modules so they see the new path.
* Reset module-level state between tests (scheduler thread handle,
  orphan registry, stop event) so each test starts from a clean slate
  regardless of run order. v3.1.10.
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
        # Reset scheduler_state singleton to v3 defaults.
        # v3.1.10: also clears cycle_started_at (added for the watchdog).
        c.execute(
            "UPDATE scheduler_state SET "
            "running=0, interval_sec=3600, last_run_at=NULL, "
            "next_run_at=NULL, last_heartbeat=NULL, "
            "consecutive_failures=0, last_error=NULL, "
            "autotrade_enabled=1, autoexit_enabled=1, kill_switch=0, "
            "exploration_mode=1, exploration_trades_target=50, "
            "owner_pid=0, cycle_started_at=NULL "
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

    # v3.1.10: reset scheduler module-level state so per-test runs don't
    # leak threads/orphans/stop signals across each other. Without this,
    # tests like test_start_and_stop_idempotent failed when run alone
    # because a previous test's _THREAD handle stayed set.
    try:
        import scheduler
        # Best-effort: signal any live thread to exit, then drop the handle.
        try:
            scheduler._STOP_EVENT.set()
        except Exception:
            pass
        try:
            if scheduler._THREAD is not None and scheduler._THREAD.is_alive():
                scheduler._THREAD.join(timeout=2)
        except Exception:
            pass
        scheduler._THREAD = None
        try:
            scheduler._STOP_EVENT.clear()
        except Exception:
            pass
        try:
            scheduler._ORPHANED_THREAD_IDS.clear()
        except Exception:
            pass
        # Reset the once-per-process silent-exit log latch
        if hasattr(scheduler._loop, "_silent_exit_logged"):
            try:
                delattr(scheduler._loop, "_silent_exit_logged")
            except Exception:
                pass
        # v3.1.10: tear down any watchdog from prior test
        try:
            scheduler._stop_watchdog()
        except Exception:
            pass
    except Exception:
        # scheduler may not be importable in some collection paths; skip
        pass
