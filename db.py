# db.py
"""
SQLite persistence layer for BursaAI.

Single connection-per-call pattern with WAL mode for concurrent read safety.
Replaces the scattered JSON files that previously caused race conditions
in the multi-threaded scheduler.

Tables
------
trades                — full trade journal (entry + exits + reasoning)
partial_exits         — per-TP partial exit records
account               — virtual paper-trade account state (single row)
parameters            — current scanner / risk params (json blob)
parameter_history     — every parameter change with timestamp + reason
bias_state            — strategy / sector bias multipliers (single row blob)
bias_history          — every bias update with before/after
state_priors          — Bayesian per-(state,action) Beta(alpha,beta) priors
learning_events       — high-level learning journal
scheduler_log         — robo-trader heartbeat + scheduled-run records
scheduler_state       — single-row scheduler status (last/next run, running flag)
trade_log             — append-only trade execution audit log
data_quality_log      — issues detected during data fetch
scan_cache            — most recent screener output (json)
"""

import sqlite3
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

HOME_DIR = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME_DIR, ".bursa_agent_data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "bursa_agent.db")

# Single global lock for *write* operations only.
# SQLite WAL handles concurrent reads natively.
_WRITE_LOCK = threading.RLock()


def get_myt_now():
    return datetime.now(timezone(timedelta(hours=8)))


def myt_iso(dt=None):
    if dt is None:
        dt = get_myt_now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# CONNECTION
# ---------------------------------------------------------------------------

@contextmanager
def connect(readonly: bool = False):
    """
    Yields a sqlite3.Connection. Always uses WAL.
    Writes are wrapped in the module-level RLock so concurrent threads
    (Streamlit re-renders + scheduler) never collide.
    """
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
        return

    with _WRITE_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    name            TEXT,
    sector          TEXT,
    signal_type     TEXT,
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    shares          INTEGER NOT NULL,
    lots            INTEGER,
    cost            REAL,
    fee             REAL,
    total_outlay    REAL,
    risk_per_share  REAL,
    actual_risk_pct REAL,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    phase           TEXT DEFAULT 'FULL',
    outcome         TEXT,
    logged_at       TEXT NOT NULL,
    closed_at       TEXT,
    execution_type  TEXT DEFAULT 'MANUAL',
    market_regime   TEXT,
    regime_conviction REAL,
    confidence_score REAL,
    entry_reasoning TEXT,
    entry_indicators_json TEXT,
    trailing_stop   REAL,
    highest_price   REAL,
    lowest_price    REAL,
    mae_pct         REAL DEFAULT 0,
    mfe_pct         REAL DEFAULT 0,
    unrealized_pnl  REAL DEFAULT 0,
    realized_pnl    REAL DEFAULT 0,
    closed_pnl      REAL,
    exit_price      REAL,
    shares_remaining INTEGER NOT NULL,
    slippage_pct    REAL DEFAULT 0,
    notes           TEXT DEFAULT '',
    tags_json       TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_logged_at ON trades(logged_at);

CREATE TABLE IF NOT EXISTS partial_exits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL,
    tp_level        TEXT,
    shares_closed   INTEGER,
    exit_price      REAL,
    pnl_rm          REAL,
    net_pnl_after_fees REAL,
    exit_at         TEXT,
    reason          TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_partials_trade ON partial_exits(trade_id);

CREATE TABLE IF NOT EXISTS account (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital   REAL NOT NULL,
    cash_balance      REAL NOT NULL,
    total_equity      REAL NOT NULL,
    last_updated      TEXT
);

CREATE TABLE IF NOT EXISTS parameters (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at  TEXT NOT NULL,
    source      TEXT,
    before_json TEXT,
    after_json  TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_paramhist_at ON parameter_history(changed_at);

CREATE TABLE IF NOT EXISTS bias_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    payload         TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bias_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at  TEXT NOT NULL,
    field       TEXT,
    before_val  REAL,
    after_val   REAL,
    trade_id    INTEGER,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS idx_biashist_at ON bias_history(changed_at);

CREATE TABLE IF NOT EXISTS state_priors (
    state_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,
    alpha       REAL NOT NULL DEFAULT 1.0,
    beta        REAL NOT NULL DEFAULT 1.0,
    n_trades    INTEGER NOT NULL DEFAULT 0,
    total_r     REAL NOT NULL DEFAULT 0,
    last_updated TEXT,
    PRIMARY KEY (state_id, action)
);

CREATE TABLE IF NOT EXISTS learning_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    description     TEXT,
    changes_json    TEXT,
    metrics_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_learnev_at ON learning_events(timestamp);

CREATE TABLE IF NOT EXISTS scheduler_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'INFO',
    event           TEXT NOT NULL,
    message         TEXT,
    duration_sec    REAL,
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedlog_at ON scheduler_log(timestamp);

CREATE TABLE IF NOT EXISTS scheduler_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    running         INTEGER NOT NULL DEFAULT 0,
    interval_sec    INTEGER NOT NULL DEFAULT 3600,
    last_run_at     TEXT,
    next_run_at     TEXT,
    last_heartbeat  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    autotrade_enabled INTEGER NOT NULL DEFAULT 1,
    autoexit_enabled  INTEGER NOT NULL DEFAULT 1,
    kill_switch     INTEGER NOT NULL DEFAULT 0,
    exploration_mode INTEGER NOT NULL DEFAULT 1,
    exploration_trades_target INTEGER NOT NULL DEFAULT 50,
    owner_pid INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event           TEXT NOT NULL,
    trade_id        INTEGER,
    ticker          TEXT,
    actor           TEXT NOT NULL DEFAULT 'USER',
    payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tradelog_at ON trade_log(timestamp);

CREATE TABLE IF NOT EXISTS data_quality_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    ticker          TEXT,
    severity        TEXT,
    issue           TEXT,
    detail_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_at ON data_quality_log(timestamp);

CREATE TABLE IF NOT EXISTS scan_cache (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT NOT NULL,
    market_regime_json TEXT,
    updated_at  TEXT NOT NULL
);

-- v3.1: Live trigger / notification system

CREATE TABLE IF NOT EXISTS live_trigger_config (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    enabled                  INTEGER NOT NULL DEFAULT 0,
    min_confidence           REAL    NOT NULL DEFAULT 70.0,
    exploit_mode_only        INTEGER NOT NULL DEFAULT 0,
    alert_on_entry           INTEGER NOT NULL DEFAULT 1,
    alert_on_full_exit       INTEGER NOT NULL DEFAULT 1,
    alert_on_stop_loss       INTEGER NOT NULL DEFAULT 1,
    alert_on_trailing_stop   INTEGER NOT NULL DEFAULT 1,
    alert_on_partial_exit    INTEGER NOT NULL DEFAULT 0,
    alert_on_risk_rejected   INTEGER NOT NULL DEFAULT 0,
    telegram_enabled         INTEGER NOT NULL DEFAULT 1,
    email_enabled            INTEGER NOT NULL DEFAULT 0,
    email_recipients         TEXT    NOT NULL DEFAULT '',
    actor_filter             TEXT    NOT NULL DEFAULT 'AGENT',
    updated_at               TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    trade_id       INTEGER,
    ticker         TEXT,
    channel        TEXT,
    status         TEXT,
    message        TEXT,
    error          TEXT,
    payload_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_at ON alert_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_alert_trade ON alert_log(trade_id);

-- v3.1.1: Idempotency guard for daily maintenance tasks
CREATE TABLE IF NOT EXISTS maintenance_state (
    task_name      TEXT PRIMARY KEY,
    last_ran_date  TEXT NOT NULL,   -- YYYY-MM-DD (MYT)
    last_ran_at    TEXT NOT NULL,   -- full timestamp
    owner_pid      INTEGER,
    result         TEXT
);

-- v3.1.4: regime history for trend analysis in cycle explanations
CREATE TABLE IF NOT EXISTS regime_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    regime          TEXT NOT NULL,
    conviction      REAL NOT NULL,
    trend_score     REAL,
    ema_200_vs_price REAL,
    klci_rsi        REAL
);
CREATE INDEX IF NOT EXISTS idx_regime_at ON regime_history(timestamp);
"""


def init_db():
    """Create tables if missing, run column migrations, and seed singleton rows."""
    with connect() as c:
        c.executescript(SCHEMA)
        # ---- Lightweight column migrations (v2 → v3 → v3.1) ----
        for sql in (
            "ALTER TABLE scheduler_state ADD COLUMN exploration_mode INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE scheduler_state ADD COLUMN exploration_trades_target INTEGER NOT NULL DEFAULT 50",
            "ALTER TABLE scheduler_state ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN executed_in_window TEXT",
        ):
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists
        # Seed scheduler_state
        c.execute(
            "INSERT OR IGNORE INTO scheduler_state "
            "(id, running, interval_sec, autotrade_enabled, autoexit_enabled, "
            " exploration_mode, exploration_trades_target) "
            "VALUES (1, 0, 3600, 1, 1, 1, 50)"
        )
        # Seed account
        c.execute(
            "INSERT OR IGNORE INTO account "
            "(id, initial_capital, cash_balance, total_equity, last_updated) "
            "VALUES (1, 20000.0, 20000.0, 20000.0, ?)",
            (myt_iso(),),
        )
        # Seed parameters
        param_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ai_parameters.json"
        )
        default_params = {
            "ema_trend": 200, "ema_fast": 10, "ema_slow": 20,
            "rsi_oversold_pullback": 40.0, "rsi_overbought": 70.0,
            "volume_surge_ratio": 1.5, "breakout_period": 20,
            "atr_period": 14, "atr_multiplier_stop": 1.5,
            "min_price": 0.30, "max_price": 4.00,
        }
        if os.path.exists(param_path):
            try:
                with open(param_path) as f:
                    default_params.update(json.load(f))
            except Exception:
                pass
        c.execute(
            "INSERT OR IGNORE INTO parameters (id, payload, updated_at) VALUES (1, ?, ?)",
            (json.dumps(default_params), myt_iso()),
        )
        # Seed bias_state
        default_bias = {
            "breakout_bias": 1.0, "pullback_bias": 1.0,
            "sector_biases": {}, "system_win_rate": 0.5,
            "strategy_stats": {}, "sector_stats": {},
            "total_closed_trades": 0,
        }
        c.execute(
            "INSERT OR IGNORE INTO bias_state (id, payload, updated_at) VALUES (1, ?, ?)",
            (json.dumps(default_bias), myt_iso()),
        )
        # v3.1: seed live trigger config (disabled by default)
        c.execute(
            "INSERT OR IGNORE INTO live_trigger_config "
            "(id, enabled, updated_at) VALUES (1, 0, ?)",
            (myt_iso(),),
        )


# ---------------------------------------------------------------------------
# Helpers exposed to callers
# ---------------------------------------------------------------------------


def execute(sql, args=()):
    with connect() as c:
        return c.execute(sql, args)


def executemany(sql, args_list):
    with connect() as c:
        return c.executemany(sql, args_list)


# Initialize on import so callers never have to remember.
try:
    init_db()
except Exception as e:
    print(f"[db] init warning: {e}")
