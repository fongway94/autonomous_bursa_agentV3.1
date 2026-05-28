# logger.py
"""
Centralised logging facade for BursaAI.

Wraps three log streams:
  - trade_log         : per-execution audit (entry, partial exit, full exit, risk reject)
  - scheduler_log     : robo-trader heartbeat + scheduled-run events
  - learning_events   : parameter / bias / model changes
  - data_quality_log  : ticker-level data issues

Also writes a parallel rotating text file for ops debugging.
"""

import os
import json
import logging
from logging.handlers import RotatingFileHandler

from db import connect, myt_iso, DATA_DIR

# -------------------------------------------------------------------------
# Text log (rotating, plain-text, ops-friendly)
# -------------------------------------------------------------------------

LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_app_logger = logging.getLogger("bursa_ai")
if not _app_logger.handlers:
    _app_logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "bursa_agent.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
    )
    fh.setFormatter(fmt)
    _app_logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    _app_logger.addHandler(ch)


def get_logger(name: str):
    return logging.getLogger(f"bursa_ai.{name}")


# -------------------------------------------------------------------------
# DB log writers
# -------------------------------------------------------------------------

def log_trade_event(event: str, trade_id=None, ticker=None,
                    actor: str = "USER", payload: dict | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO trade_log (timestamp, event, trade_id, ticker, actor, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (myt_iso(), event, trade_id, ticker, actor,
             json.dumps(payload or {}, default=str)),
        )
    _app_logger.info(f"TRADE {event} | trade_id={trade_id} ticker={ticker} actor={actor}")


def log_scheduler_event(event: str, message: str = "",
                        level: str = "INFO", duration_sec: float | None = None,
                        payload: dict | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO scheduler_log "
            "(timestamp, level, event, message, duration_sec, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (myt_iso(), level, event, message, duration_sec,
             json.dumps(payload or {}, default=str)),
        )
    if level == "ERROR":
        _app_logger.error(f"SCHEDULER {event} | {message}")
    elif level == "WARN":
        _app_logger.warning(f"SCHEDULER {event} | {message}")
    else:
        _app_logger.info(f"SCHEDULER {event} | {message}")


def log_learning_event(event_type: str, description: str,
                       changes: dict | None = None, metrics: dict | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO learning_events "
            "(timestamp, event_type, description, changes_json, metrics_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (myt_iso(), event_type, description,
             json.dumps(changes or {}, default=str),
             json.dumps(metrics or {}, default=str)),
        )
    _app_logger.info(f"LEARNING {event_type} | {description}")


def log_parameter_change(before: dict, after: dict, source: str, reason: str = ""):
    with connect() as c:
        c.execute(
            "INSERT INTO parameter_history "
            "(changed_at, source, before_json, after_json, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (myt_iso(), source, json.dumps(before, default=str),
             json.dumps(after, default=str), reason),
        )
    _app_logger.info(f"PARAM_CHANGE source={source} reason={reason}")


def log_bias_change(field: str, before: float, after: float,
                    trade_id: int | None = None, outcome: str | None = None):
    if abs((after or 0) - (before or 0)) < 1e-6:
        return
    with connect() as c:
        c.execute(
            "INSERT INTO bias_history "
            "(changed_at, field, before_val, after_val, trade_id, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (myt_iso(), field, before, after, trade_id, outcome),
        )
    _app_logger.info(
        f"BIAS_CHANGE {field}: {before:.3f} -> {after:.3f} "
        f"(trade {trade_id}, {outcome})"
    )


def log_data_quality(ticker: str, issue: str,
                     severity: str = "WARN", detail: dict | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO data_quality_log "
            "(timestamp, ticker, severity, issue, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (myt_iso(), ticker, severity, issue,
             json.dumps(detail or {}, default=str)),
        )
    if severity == "ERROR":
        _app_logger.error(f"DATA_QUALITY {ticker} {issue}")
    elif severity == "WARN":
        _app_logger.warning(f"DATA_QUALITY {ticker} {issue}")


# -------------------------------------------------------------------------
# Read helpers (for dashboard)
# -------------------------------------------------------------------------

def get_trade_log(limit: int = 200, event_filter: str | None = None,
                  actor_filter: str | None = None):
    sql = "SELECT * FROM trade_log WHERE 1=1"
    args = []
    if event_filter:
        sql += " AND event = ?"
        args.append(event_filter)
    if actor_filter:
        sql += " AND actor = ?"
        args.append(actor_filter)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_scheduler_log(limit: int = 200, level: str | None = None):
    sql = "SELECT * FROM scheduler_log WHERE 1=1"
    args = []
    if level:
        sql += " AND level = ?"
        args.append(level)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def get_learning_events(limit: int = 100):
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM learning_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


def get_parameter_history(limit: int = 100):
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM parameter_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


def get_bias_history(limit: int = 200):
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM bias_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


def get_data_quality_log(limit: int = 200):
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM data_quality_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


# -------------------------------------------------------------------------
# Maintenance
# -------------------------------------------------------------------------

def prune_logs(max_rows_per_table: int = 5000):
    """Keep tables bounded — call from scheduler once a day."""
    with connect() as c:
        for tbl in ("scheduler_log", "trade_log", "data_quality_log",
                    "learning_events", "parameter_history", "bias_history"):
            row = c.execute(f"SELECT MAX(id) FROM {tbl}").fetchone()
            if not row or row[0] is None:
                continue
            cutoff_id = row[0] - max_rows_per_table
            if cutoff_id > 0:
                c.execute(f"DELETE FROM {tbl} WHERE id <= ?", (cutoff_id,))
    _app_logger.info(f"Pruned all log tables to last {max_rows_per_table} rows")


def dedupe_scheduler_log_at_same_second(keep_latest: bool = True) -> int:
    """
    Remove duplicate scheduler_log rows.

    Two sweeps:
      1. Strict dedup: collapse rows with identical (timestamp, event, message).
         Catches HEARTBEAT/SKIP multiplications from ghost threads.
      2. Daily-task dedup: for NIGHTLY_RETRAIN / EXPLORATION_END events,
         collapse multiple rows on the same MYT calendar day to just one.
         Catches the case where ghost threads each produced *different*
         OOS accuracy numbers but it was still semantically one event.

    Returns the total number of rows deleted. Cheap one-time cleanup —
    safe to call repeatedly (idempotent once duplicates are gone).
    """
    with connect() as c:
        before = c.execute("SELECT COUNT(*) FROM scheduler_log").fetchone()[0]

        # Sweep 1: identical (timestamp, event, message)
        c.execute(
            "DELETE FROM scheduler_log WHERE id NOT IN ("
            "  SELECT MIN(id) FROM scheduler_log "
            "  GROUP BY timestamp, event, message"
            ")"
        )

        # Sweep 2: per-day idempotent events — keep only the first row
        # per (date, event_type) for events that should fire once a day.
        c.execute(
            "DELETE FROM scheduler_log WHERE id NOT IN ("
            "  SELECT MIN(id) FROM scheduler_log "
            "  GROUP BY substr(timestamp,1,10), event"
            ") AND event IN ('NIGHTLY_RETRAIN', 'EXPLORATION_END', "
            "                'MAINTENANCE_ERROR')"
        )

        after = c.execute("SELECT COUNT(*) FROM scheduler_log").fetchone()[0]
    removed = before - after
    if removed > 0:
        _app_logger.info(f"deduped scheduler_log: removed {removed} duplicate rows")
    return removed
