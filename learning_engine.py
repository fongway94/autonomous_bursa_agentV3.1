# learning_engine.py
"""
DEPRECATED — kept only as a thin compatibility shim.

The original ~1200-line learning_engine has been split into the v2 modules:
  * learner.py            — Bayesian state priors + walk-forward + ML classifier
  * scheduler.py          — background Robo-Trader loop
  * trading_engine.py     — execute_entry / partial / full exits + auto_settle
  * logger.py             — learning_events, scheduler_log, trade_log, bias_history

All public symbols below proxy to the new modules so that any leftover
import in user code (or a previous app.py) doesn't crash.
"""

import warnings

warnings.warn(
    "learning_engine.py is deprecated. Use learner / scheduler / "
    "trading_engine / logger instead.",
    DeprecationWarning, stacklevel=2,
)

# Re-exports
from learner import (
    learn_from_trade_outcome,
    run_walk_forward_optimization,
    train_setup_classifier,
    get_ml_score,
    get_learning_history,
    get_strategy_performance_report,
)
from scheduler import (
    start as start_background_trader,
    stop as stop_background_trader,
    force_restart as force_reboot_scheduler,
    is_running as is_scheduler_running,
    run_once as check_and_execute_passive_scheduler,
)
from trading_engine import auto_settle_trades as auto_manage_portfolio
from logger import get_scheduler_log as load_scheduler_state
