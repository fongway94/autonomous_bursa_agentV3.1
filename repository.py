# repository.py
"""
Repository pattern over SQLite.

Keeps SQL out of the engines. Every other module talks to trades/account/params
through these functions. Backwards-compatible accessors (`load_trades`,
`save_account` etc.) are provided so the existing app.py can be migrated in
small steps.
"""

import json
from typing import Any
from datetime import datetime

from db import connect, myt_iso


# =========================================================================
# ACCOUNT
# =========================================================================

def load_account() -> dict:
    with connect(readonly=True) as c:
        row = c.execute("SELECT * FROM account WHERE id = 1").fetchone()
    if row is None:
        return {
            "initial_capital": 20000.0, "cash_balance": 20000.0,
            "total_equity": 20000.0, "last_updated": myt_iso(),
        }
    return dict(row)


def save_account(initial_capital=None, cash_balance=None,
                 total_equity=None) -> None:
    current = load_account()
    payload = {
        "initial_capital": initial_capital if initial_capital is not None
        else current["initial_capital"],
        "cash_balance": cash_balance if cash_balance is not None
        else current["cash_balance"],
        "total_equity": total_equity if total_equity is not None
        else current["total_equity"],
        "last_updated": myt_iso(),
    }
    with connect() as c:
        c.execute(
            "UPDATE account SET initial_capital=?, cash_balance=?, "
            "total_equity=?, last_updated=? WHERE id=1",
            (payload["initial_capital"], payload["cash_balance"],
             payload["total_equity"], payload["last_updated"]),
        )


def reset_account(initial_capital: float = 20000.0) -> None:
    with connect() as c:
        c.execute(
            "UPDATE account SET initial_capital=?, cash_balance=?, "
            "total_equity=?, last_updated=? WHERE id=1",
            (initial_capital, initial_capital, initial_capital, myt_iso()),
        )


# =========================================================================
# PARAMETERS
# =========================================================================

def load_parameters() -> dict:
    with connect(readonly=True) as c:
        row = c.execute("SELECT payload FROM parameters WHERE id=1").fetchone()
    if row is None:
        return {
            "ema_trend": 200, "ema_fast": 10, "ema_slow": 20,
            "rsi_oversold_pullback": 40.0, "rsi_overbought": 70.0,
            "volume_surge_ratio": 1.5, "breakout_period": 20,
            "atr_period": 14, "atr_multiplier_stop": 1.5,
            "min_price": 0.30, "max_price": 4.00,
        }
    return json.loads(row["payload"])


def save_parameters(params: dict, source: str = "USER", reason: str = "") -> None:
    from logger import log_parameter_change
    before = load_parameters()
    with connect() as c:
        c.execute(
            "UPDATE parameters SET payload=?, updated_at=? WHERE id=1",
            (json.dumps(params), myt_iso()),
        )
    log_parameter_change(before, params, source, reason)


# =========================================================================
# BIAS STATE
# =========================================================================

def load_bias_state() -> dict:
    with connect(readonly=True) as c:
        row = c.execute("SELECT payload FROM bias_state WHERE id=1").fetchone()
    if row is None:
        return {
            "breakout_bias": 1.0, "pullback_bias": 1.0,
            "sector_biases": {}, "system_win_rate": 0.5,
            "strategy_stats": {}, "sector_stats": {},
            "total_closed_trades": 0,
        }
    return json.loads(row["payload"])


def save_bias_state(payload: dict) -> None:
    with connect() as c:
        c.execute(
            "UPDATE bias_state SET payload=?, updated_at=? WHERE id=1",
            (json.dumps(payload), myt_iso()),
        )


# =========================================================================
# TRADES
# =========================================================================

_TRADE_COLS = [
    "id", "ticker", "name", "sector", "signal_type",
    "entry_price", "stop_loss", "tp1", "tp2", "tp3",
    "shares", "lots", "cost", "fee", "total_outlay",
    "risk_per_share", "actual_risk_pct",
    "status", "phase", "outcome",
    "logged_at", "closed_at", "execution_type",
    "market_regime", "regime_conviction", "confidence_score",
    "entry_reasoning", "entry_indicators_json",
    "trailing_stop", "highest_price", "lowest_price",
    "mae_pct", "mfe_pct",
    "unrealized_pnl", "realized_pnl", "closed_pnl",
    "exit_price", "shares_remaining", "slippage_pct",
    "notes", "tags_json",
]


def _row_to_trade(row) -> dict:
    if row is None:
        return None
    t = dict(row)
    if t.get("entry_indicators_json"):
        try:
            t["entry_indicators"] = json.loads(t["entry_indicators_json"])
        except Exception:
            t["entry_indicators"] = {}
    else:
        t["entry_indicators"] = {}
    if t.get("tags_json"):
        try:
            t["tags"] = json.loads(t["tags_json"])
        except Exception:
            t["tags"] = []
    else:
        t["tags"] = []
    return t


def insert_trade(trade: dict) -> int:
    """Insert a trade. Returns the new trade_id."""
    payload = {k: trade.get(k) for k in _TRADE_COLS if k != "id"}
    payload["entry_indicators_json"] = json.dumps(
        trade.get("entry_indicators", {}), default=str
    )
    payload["tags_json"] = json.dumps(trade.get("tags", []), default=str)
    if not payload.get("logged_at"):
        payload["logged_at"] = myt_iso()
    if payload.get("shares_remaining") is None:
        payload["shares_remaining"] = payload.get("shares", 0)

    cols = ",".join(payload.keys())
    placeholders = ",".join("?" * len(payload))
    with connect() as c:
        cur = c.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        return cur.lastrowid


def update_trade(trade_id: int, fields: dict) -> None:
    if not fields:
        return
    if "entry_indicators" in fields:
        fields["entry_indicators_json"] = json.dumps(
            fields.pop("entry_indicators"), default=str
        )
    if "tags" in fields:
        fields["tags_json"] = json.dumps(fields.pop("tags"), default=str)
    set_clause = ",".join(f"{k}=?" for k in fields.keys())
    args = tuple(fields.values()) + (trade_id,)
    with connect() as c:
        c.execute(f"UPDATE trades SET {set_clause} WHERE id=?", args)


def get_trade(trade_id: int) -> dict | None:
    with connect(readonly=True) as c:
        row = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return _row_to_trade(row)


def load_trades(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM trades"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY id ASC"
    with connect(readonly=True) as c:
        return [_row_to_trade(r) for r in c.execute(sql, args).fetchall()]


def active_trades() -> list[dict]:
    return load_trades(status="ACTIVE")


def closed_trades() -> list[dict]:
    return load_trades(status="CLOSED")


def insert_partial_exit(trade_id: int, partial: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO partial_exits "
            "(trade_id, tp_level, shares_closed, exit_price, pnl_rm, "
            " net_pnl_after_fees, exit_at, reason) VALUES (?,?,?,?,?,?,?,?)",
            (trade_id, partial.get("tp_level"), partial.get("shares_closed"),
             partial.get("exit_price"), partial.get("pnl_rm"),
             partial.get("net_pnl_after_fees"),
             partial.get("exit_at", myt_iso()), partial.get("reason", "")),
        )


def get_partial_exits(trade_id: int) -> list[dict]:
    with connect(readonly=True) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM partial_exits WHERE trade_id=? ORDER BY id ASC",
            (trade_id,),
        ).fetchall()]


# =========================================================================
# SCAN CACHE
# =========================================================================

def save_scan_cache(records: list[dict], market_regime: dict | None = None) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO scan_cache (id, payload, market_regime_json, updated_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
            "market_regime_json=excluded.market_regime_json, "
            "updated_at=excluded.updated_at",
            (json.dumps(records, default=str),
             json.dumps(market_regime or {}, default=str),
             myt_iso()),
        )


def load_scan_cache() -> tuple[list[dict], dict, str | None]:
    with connect(readonly=True) as c:
        row = c.execute("SELECT * FROM scan_cache WHERE id=1").fetchone()
    if row is None:
        return [], {}, None
    try:
        records = json.loads(row["payload"])
        regime = json.loads(row["market_regime_json"] or "{}")
    except Exception:
        records, regime = [], {}
    return records, regime, row["updated_at"]


# =========================================================================
# SCHEDULER STATE
# =========================================================================

def get_scheduler_state() -> dict:
    with connect(readonly=True) as c:
        row = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    return dict(row) if row else {}


def update_scheduler_state(**fields) -> None:
    if not fields:
        return
    set_clause = ",".join(f"{k}=?" for k in fields.keys())
    args = tuple(fields.values())
    with connect() as c:
        c.execute(f"UPDATE scheduler_state SET {set_clause} WHERE id=1", args)

# =========================================================================
# DAILY MAINTENANCE IDEMPOTENCY (v3.1.1)
# =========================================================================

def try_claim_daily_task(task_name: str, owner_pid: int | None = None) -> bool:
    """
    Atomically claim a daily maintenance task for today (MYT date).

    Returns True if this caller is the first to claim it today —
    in which case they should proceed to run the task.
    Returns False if any other caller has already claimed it today
    (within this process or any ghost thread/sibling process).

    Uses INSERT ... ON CONFLICT DO NOTHING for atomic CAS semantics
    on the (task_name) primary key.
    """
    import os
    from datetime import datetime, timezone, timedelta
    pid = owner_pid if owner_pid is not None else os.getpid()
    myt_zone = timezone(timedelta(hours=8))
    today = datetime.now(myt_zone).strftime("%Y-%m-%d")
    now = datetime.now(myt_zone).strftime("%Y-%m-%d %H:%M:%S")

    with connect() as c:
        # Step 1: try INSERT — succeeds only if no row exists for this task
        cur = c.execute(
            "INSERT OR IGNORE INTO maintenance_state "
            "(task_name, last_ran_date, last_ran_at, owner_pid) "
            "VALUES (?, ?, ?, ?)",
            (task_name, today, now, pid),
        )
        if cur.rowcount == 1:
            return True  # we just inserted — we own today's run

        # Step 2: row exists; CAS update from yesterday → today
        cur = c.execute(
            "UPDATE maintenance_state "
            "SET last_ran_date=?, last_ran_at=?, owner_pid=? "
            "WHERE task_name=? AND last_ran_date < ?",
            (today, now, pid, task_name, today),
        )
        return cur.rowcount == 1


def record_daily_task_result(task_name: str, result: str) -> None:
    """Optional: store the outcome of a daily task for inspection."""
    with connect() as c:
        c.execute(
            "UPDATE maintenance_state SET result=? WHERE task_name=?",
            (result, task_name),
        )

# =========================================================================
# REGIME HISTORY (v3.1.4) — for trend analysis in cycle explanations
# =========================================================================

def record_regime_snapshot(regime: str, conviction: float,
                            trend_score: float | None = None,
                            ema_200_vs_price: float | None = None,
                            klci_rsi: float | None = None) -> None:
    """
    Append a regime snapshot. Called once per cycle by the scheduler.

    Each row captures (timestamp, regime, conviction, key indicators) so
    we can later compute trend deltas like "BEAR conviction was 50% an
    hour ago, now 40% → weakening; entries may resume soon".
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S")
    with connect() as c:
        c.execute(
            "INSERT INTO regime_history "
            "(timestamp, regime, conviction, trend_score, "
            " ema_200_vs_price, klci_rsi) VALUES (?,?,?,?,?,?)",
            (now, regime, float(conviction),
             float(trend_score) if trend_score is not None else None,
             float(ema_200_vs_price) if ema_200_vs_price is not None else None,
             float(klci_rsi) if klci_rsi is not None else None),
        )


def get_regime_trend(lookback_hours: int = 24) -> dict:
    """
    Returns a summary of how the regime has evolved over the last N hours.

    {
      "current_conviction": float,
      "avg_recent_conviction": float,
      "change": float,                # current - avg_recent
      "direction": "STRENGTHENING" | "WEAKENING" | "STABLE",
      "ema_200_distance_pct": float | None,
      "samples": int
    }
    """
    from datetime import datetime, timezone, timedelta
    myt = timezone(timedelta(hours=8))
    cutoff = (datetime.now(myt) - timedelta(hours=lookback_hours)
              ).strftime("%Y-%m-%d %H:%M:%S")
    with connect(readonly=True) as c:
        rows = c.execute(
            "SELECT regime, conviction, ema_200_vs_price "
            "FROM regime_history "
            "WHERE timestamp >= ? ORDER BY id ASC",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {"current_conviction": None, "avg_recent_conviction": None,
                "change": None, "direction": "UNKNOWN",
                "ema_200_distance_pct": None, "samples": 0}

    current = rows[-1]
    current_conv = float(current["conviction"])

    # Skip the very latest row when computing the "recent" average,
    # so the comparison is current vs prior trend.
    prior = rows[:-1] if len(rows) > 1 else rows
    avg_prior = float(sum(r["conviction"] for r in prior) / len(prior))

    change = current_conv - avg_prior
    if abs(change) < 3:
        direction = "STABLE"
    elif change > 0:
        # In BEAR, rising conviction = BEAR getting stronger (bad)
        # In BULL, rising conviction = BULL getting stronger (good)
        direction = "STRENGTHENING"
    else:
        direction = "WEAKENING"

    return {
        "current_conviction": round(current_conv, 1),
        "avg_recent_conviction": round(avg_prior, 1),
        "change": round(change, 1),
        "direction": direction,
        "ema_200_distance_pct": (round(float(current["ema_200_vs_price"]), 2)
                                  if current["ema_200_vs_price"] is not None
                                  else None),
        "samples": len(rows),
    }
