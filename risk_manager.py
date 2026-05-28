# risk_manager.py
"""
Risk Manager — Central risk control hub.

Fixed bugs from v1
------------------
* `size_multiplier` is now ACTUALLY enforced by callers (engines respect it).
* `check_risk_amount.adjusted_shares` removed (was nonsense formula).
* `position_limit_check` returns `allowed=False` when *no* size reduction
  would bring it under cap (was always True).
* Risk params persisted in DB.
"""

from __future__ import annotations
import json
from typing import Any

from db import connect, myt_iso, get_myt_now


DEFAULT_RISK_PARAMS = {
    "max_drawdown_pct": 8.0,
    "max_drawdown_strict_pct": 15.0,
    "min_risk_per_trade_rm": 50.0,
    "max_risk_per_trade_pct": 1.0,  # v3: safer default for auto-trade
    "max_position_cost_pct": 20.0,
    "max_sector_exposure_pct": 40.0,
    "max_concurrent_positions": 8,
    "max_correlation_threshold": 0.7,
    "max_trades_per_day": 5,
    "min_trades_per_week": 0,
    "no_entry_before_time": "09:00",  # Bursa morning open
    "no_entry_after_time": "17:00",   # Bursa TaL close
    "max_stop_loss_pct": 10.0,
    "min_stop_loss_pct": 1.5,
    "trailing_stop_activation": "TP1",
    "trailing_stop_buffer_pct": 0.5,
    "enforce_lot_size": True,
}


def _ensure_risk_row():
    with connect() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='risk_params'"
        ).fetchone()
        if not row:
            c.execute("CREATE TABLE IF NOT EXISTS risk_params "
                      "(id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT, "
                      "updated_at TEXT)")
            c.execute("INSERT OR IGNORE INTO risk_params (id, payload, updated_at) "
                      "VALUES (1, ?, ?)",
                      (json.dumps(DEFAULT_RISK_PARAMS), myt_iso()))


def load_risk_params() -> dict:
    _ensure_risk_row()
    with connect(readonly=True) as c:
        row = c.execute("SELECT payload FROM risk_params WHERE id=1").fetchone()
    params = DEFAULT_RISK_PARAMS.copy()
    if row:
        try:
            params.update(json.loads(row["payload"]))
        except Exception:
            pass
    return params


def save_risk_params(params: dict) -> None:
    _ensure_risk_row()
    merged = DEFAULT_RISK_PARAMS.copy()
    merged.update(params or {})
    with connect() as c:
        c.execute("UPDATE risk_params SET payload=?, updated_at=? WHERE id=1",
                  (json.dumps(merged), myt_iso()))


# -------------------------------------------------------------------------
# Individual checks
# -------------------------------------------------------------------------

def check_drawdown_circuit_breaker(initial_capital: float,
                                   current_equity: float) -> dict:
    if initial_capital <= 0:
        return {"allowed": True, "level": "NONE", "reason": "No capital data",
                "pct_drop": 0}
    pct = (initial_capital - current_equity) / initial_capital * 100
    p = load_risk_params()
    if pct >= p["max_drawdown_strict_pct"]:
        return {"allowed": False, "level": "STRICT_CIRCUIT_BREAKER",
                "reason": f"Equity dropped {pct:.1f}% (limit "
                          f"{p['max_drawdown_strict_pct']}%). ALL trading paused.",
                "pct_drop": round(pct, 2)}
    if pct >= p["max_drawdown_pct"]:
        return {"allowed": True, "level": "WARN_DRAWDOWN",
                "reason": f"Equity dropped {pct:.1f}% (warn at "
                          f"{p['max_drawdown_pct']}%). New positions at 50% size.",
                "pct_drop": round(pct, 2)}
    return {"allowed": True, "level": "OK",
            "reason": f"Drawdown {pct:.1f}% (within limits).",
            "pct_drop": round(pct, 2)}


def check_position_limits(trades: list, new_trade_cost: float,
                          sector: str, capital: float) -> dict:
    p = load_risk_params()
    max_pos = p["max_concurrent_positions"]
    max_cost_pct = p["max_position_cost_pct"]
    max_sec_pct = p["max_sector_exposure_pct"]
    active = [t for t in trades if t.get("status") == "ACTIVE"]

    if len(active) >= max_pos:
        return {"allowed": False,
                "reason": f"Max {max_pos} concurrent positions reached.",
                "size_reduction_pct": 0}

    max_cost = capital * (max_cost_pct / 100)
    if new_trade_cost > max_cost:
        reduce_pct = min((new_trade_cost - max_cost) / new_trade_cost * 100, 80)
        if reduce_pct >= 80:
            return {"allowed": False,
                    "reason": f"Position cost RM {new_trade_cost:,.0f} "
                              f"hugely over {max_cost_pct}% cap.",
                    "size_reduction_pct": 100}
        return {"allowed": True,
                "reason": f"Position size > {max_cost_pct}% cap. "
                          f"Reduced {reduce_pct:.0f}%.",
                "size_reduction_pct": reduce_pct}

    sector_cost = sum(t.get("cost", 0) for t in active
                      if t.get("sector") == sector)
    sec_cap = capital * (max_sec_pct / 100)
    if sector_cost + new_trade_cost > sec_cap:
        avail = sec_cap - sector_cost
        if avail <= 0:
            return {"allowed": False,
                    "reason": f"Sector '{sector}' exposure already at cap "
                              f"({sector_cost / capital * 100:.1f}%/{max_sec_pct}%).",
                    "size_reduction_pct": 100}
        reduce_pct = (new_trade_cost - avail) / new_trade_cost * 100
        return {"allowed": True,
                "reason": f"Sector cap reached — reduce {reduce_pct:.0f}%.",
                "size_reduction_pct": reduce_pct}

    return {"allowed": True, "reason": "All position limits OK.",
            "size_reduction_pct": 0}


def check_risk_amount(trade_risk_amount: float, capital: float) -> dict:
    p = load_risk_params()
    min_r = p["min_risk_per_trade_rm"]
    max_r = capital * (p["max_risk_per_trade_pct"] / 100)
    if trade_risk_amount < min_r:
        return {"allowed": False,
                "reason": f"Risk RM {trade_risk_amount:.2f} below min RM {min_r:.2f}."}
    if trade_risk_amount > max_r:
        return {"allowed": True,
                "reason": f"Risk capped at RM {max_r:.2f} "
                          f"({p['max_risk_per_trade_pct']}% of capital)."}
    return {"allowed": True, "reason": "Risk amount OK."}


def check_daily_trade_limit(trades: list) -> dict:
    p = load_risk_params()
    today = get_myt_now().strftime("%Y-%m-%d")
    n = sum(1 for t in trades if (t.get("logged_at") or "").startswith(today)
            and t.get("status") != "REJECTED")
    if n >= p["max_trades_per_day"]:
        return {"allowed": False,
                "reason": f"Daily trade limit ({p['max_trades_per_day']}) reached.",
                "count": n}
    return {"allowed": True,
            "reason": f"{p['max_trades_per_day'] - n} trades remaining today.",
            "count": n}


def check_trading_time_window() -> dict:
    """
    Delegate to market_calendar for accurate Bursa session handling.

    Returns the same {allowed, reason, window} dict shape as before
    (for backwards compatibility with existing callers in scheduler.py
    and app.py), but now honours:
      * Real Bursa sessions (09:00-12:30, 14:30-17:00)
      * Lunch break (12:30-14:00) blocks scans
      * Public holidays (Hari Raya, Wesak, Deepavali, etc.)
      * Safe-entry cutoff at 16:00 for new auto-entries

    User-tunable `no_entry_before_time` / `no_entry_after_time` in
    risk_params are honoured as ADDITIONAL constraints — they can
    only tighten the window, not extend it past Bursa hours.
    """
    from market_calendar import (
        is_market_open, is_safe_entry_window, market_status_text,
        current_session,
    )
    p = load_risk_params()
    now = get_myt_now()
    t = now.strftime("%H:%M")

    status = market_status_text(now)

    if not is_market_open(now):
        return {"allowed": False,
                "reason": status["reason"] + f" (next: {status['next_event']})",
                "window": status["session"]}

    # Market is open. Now apply the user's optional tighter window
    user_min = p.get("no_entry_before_time", "09:00")
    user_max = p.get("no_entry_after_time", "17:00")
    if t < user_min:
        return {"allowed": False,
                "reason": f"User-configured pre-market: opens {user_min} MYT.",
                "window": f"Before {user_min}"}
    if t > user_max:
        return {"allowed": False,
                "reason": f"User-configured cutoff: after {user_max} MYT.",
                "window": f"After {user_max}"}

    # Optionally also block new entries in the no-safe-entry tail
    if not is_safe_entry_window(now):
        sess = current_session(now)
        sess_name = sess.name if sess else "?"
        return {"allowed": False,
                "reason": (f"In {sess_name} — too late for new entries "
                           "(safe-entry window ended 16:00)."),
                "window": sess_name}

    sess = current_session(now)
    return {"allowed": True,
            "reason": f"{sess.name} session — fills active.",
            "window": f"{sess.name} ({t})"}


def validate_stop_loss(entry_price: float, proposed_sl: float) -> dict:
    p = load_risk_params()
    risk_pct = (entry_price - proposed_sl) / entry_price * 100
    if risk_pct < p["min_stop_loss_pct"]:
        adj = entry_price * (1 - p["min_stop_loss_pct"] / 100)
        return {"valid": True, "adjusted_sl": round(adj, 3),
                "reason": f"Stop too tight ({risk_pct:.2f}%) — set to "
                          f"min {p['min_stop_loss_pct']}%."}
    if risk_pct > p["max_stop_loss_pct"]:
        adj = entry_price * (1 - p["max_stop_loss_pct"] / 100)
        return {"valid": True, "adjusted_sl": round(adj, 3),
                "reason": f"Stop too wide ({risk_pct:.2f}%) — capped "
                          f"at {p['max_stop_loss_pct']}%."}
    return {"valid": True, "adjusted_sl": round(proposed_sl, 3),
            "reason": f"Stop {risk_pct:.2f}% within range."}


# -------------------------------------------------------------------------
# Aggregated check
# -------------------------------------------------------------------------

def run_full_risk_check(trades: list, new_trade_info: dict,
                        capital: float, initial_capital: float) -> dict:
    checks = {
        "drawdown_check": check_drawdown_circuit_breaker(initial_capital, capital),
        "position_limit_check": check_position_limits(
            trades, new_trade_info.get("cost", 0),
            new_trade_info.get("sector", "Unknown"), capital),
        "risk_amount_check": check_risk_amount(
            new_trade_info.get("risk_amount", 0), capital),
        "daily_limit_check": check_daily_trade_limit(trades),
        "time_window_check": check_trading_time_window(),
    }

    # Compose verdict.
    rejecting = []
    size_mult = 1.0

    dd = checks["drawdown_check"]
    if not dd["allowed"]:
        rejecting.append(f"drawdown: {dd['reason']}")
    elif dd["level"] == "WARN_DRAWDOWN":
        size_mult = min(size_mult, 0.5)

    pl = checks["position_limit_check"]
    if not pl["allowed"]:
        rejecting.append(f"position_limit: {pl['reason']}")
    elif pl["size_reduction_pct"] > 0:
        size_mult = min(size_mult, 1 - pl["size_reduction_pct"] / 100)

    if not checks["risk_amount_check"]["allowed"]:
        rejecting.append(f"risk: {checks['risk_amount_check']['reason']}")

    if not checks["daily_limit_check"]["allowed"]:
        rejecting.append(f"daily: {checks['daily_limit_check']['reason']}")

    if not checks["time_window_check"]["allowed"]:
        rejecting.append(f"time: {checks['time_window_check']['reason']}")

    passed = not rejecting
    verdict = "✅ APPROVED" if passed else "❌ REJECTED — " + "; ".join(rejecting)

    return {
        "pass": passed,
        "checks": checks,
        "final_verdict": verdict,
        "size_multiplier": round(max(size_mult, 0.0), 3),
        "risk_level": dd["level"],
    }


def get_risk_dashboard_stats(trades: list, capital: float,
                             initial_capital: float) -> dict:
    p = load_risk_params()
    active = [t for t in trades if t.get("status") == "ACTIVE"]
    dd = check_drawdown_circuit_breaker(initial_capital, capital)
    total_exp = sum(t.get("cost", 0) for t in active)
    exp_pct = (total_exp / capital * 100) if capital > 0 else 0
    sec_exp: dict[str, float] = {}
    for t in active:
        sec = t.get("sector") or "Unknown"
        sec_exp[sec] = sec_exp.get(sec, 0) + (t.get("cost") or 0)
    tw = check_trading_time_window()
    dl = check_daily_trade_limit(trades)
    return {
        "drawdown_pct": dd["pct_drop"],
        "drawdown_level": dd["level"],
        "drawdown_allowed": dd["allowed"],
        "drawdown_reason": dd["reason"],
        "total_exposure_rm": round(total_exp, 2),
        "exposure_pct": round(exp_pct, 1),
        "active_positions": len(active),
        "max_positions_allowed": p["max_concurrent_positions"],
        "sector_exposure": {k: round(v, 2) for k, v in sec_exp.items()},
        "trades_today": dl["count"],
        "trades_daily_limit": p["max_trades_per_day"],
        "trading_window": tw["window"],
        "can_trade_now": tw["allowed"] and dd["allowed"],
        "risk_params": p,
    }
