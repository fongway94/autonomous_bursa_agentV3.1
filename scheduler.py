# scheduler.py
"""
Background scheduler — Robo-Trader.

Design
------
Spawns a daemon thread inside the Streamlit process when the app boots.
The thread:
  1. Sleeps until the next cadence boundary (hourly).
  2. Wakes during market hours, runs scan → auto-settle → bookkeeping.
  3. Records HEARTBEAT every cycle into scheduler_log.
  4. Honours the `kill_switch` flag in scheduler_state.
  5. Exits cleanly when `running = 0` is set.

Thread-safety: any one Python process should run at most ONE scheduler
thread. We enforce this with a module-global handle + an atomic
INSERT-OR-UPDATE on scheduler_state.

If the user wants a real production setup, they can run
`python -m scheduler --daemon` from cron / Task Scheduler — the same
loop code is reused.
"""

from __future__ import annotations
import os
import sys
import time
import threading
import traceback
from datetime import datetime, timezone, timedelta

import pandas as pd

from db import get_myt_now, myt_iso
from repository import (
    get_scheduler_state, update_scheduler_state, save_scan_cache,
    active_trades, load_account, save_account,
)
from logger import (
    log_scheduler_event, log_learning_event, get_logger, prune_logs,
)
from risk_manager import check_trading_time_window, run_full_risk_check

log = get_logger("scheduler")

# Module-global thread handle (single-process invariant)
_THREAD: threading.Thread | None = None
_LOCK = threading.RLock()
_STOP_EVENT = threading.Event()


# -------------------------------------------------------------------------
# Cadence helpers
# -------------------------------------------------------------------------

def _next_run_at(interval_sec: int) -> datetime:
    """Next top-of-hour (if interval = 3600) or now + interval."""
    now = get_myt_now()
    if interval_sec == 3600:
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        nxt = now + timedelta(seconds=interval_sec)
    return nxt


def _is_market_hours() -> bool:
    tw = check_trading_time_window()
    return tw["allowed"]


# -------------------------------------------------------------------------
# Single cycle
# -------------------------------------------------------------------------

def _run_one_cycle(autotrade: bool, autoexit: bool) -> dict:
    """One full scan + settle cycle. Returns summary dict."""
    from screener import screen_all_stocks
    from market_analyzer import get_full_market_analysis
    from trading_engine import auto_settle_trades, execute_entry
    from learner import learn_from_trade_outcome
    from repository import get_trade

    summary = {"scan_count": 0, "settled": 0, "partials": 0,
               "auto_entries": 0, "rejected": 0, "errors": []}

    t0 = time.time()
    log_scheduler_event("SCAN_START", "Starting market scan")

    try:
        regime = get_full_market_analysis(force_refresh=True)
    except Exception as e:
        regime = {"regime_data": {"regime": "UNCERTAIN"}, "position_rules": {}}
        log_scheduler_event("ERROR", f"Regime fetch failed: {e}", "ERROR")

    try:
        df = screen_all_stocks(market_regime=regime)
        summary["scan_count"] = len(df)
        save_scan_cache(df.to_dict("records") if not df.empty else [], regime)
    except Exception as e:
        log_scheduler_event("ERROR", f"Scan failed: {e}\n{traceback.format_exc()}",
                            "ERROR")
        return summary

    duration = time.time() - t0
    log_scheduler_event("SCAN_END", f"{len(df)} signals", duration_sec=duration,
                        payload={"regime": regime.get("regime_data", {}).get("regime")})

    # ---- Auto-settle existing positions ----
    if autoexit and not df.empty:
        log_scheduler_event("SETTLE_START", "Auto-settling active trades")
        price_lookup = {}
        for _, row in df.iterrows():
            price_lookup[row["ticker"]] = {
                "price": float(row["price"]),
                "high": float(row.get("price", 0)) * 1.0,  # daily bars: high≈price
                "low": float(row.get("price", 0)) * 1.0,
            }
        try:
            settle_res = auto_settle_trades(price_lookup, regime, actor="AGENT")
            summary["settled"] = len(settle_res.get("settled", []))
            summary["partials"] = len(settle_res.get("partials", []))
            log_scheduler_event(
                "SETTLE_END",
                f"{summary['settled']} settled, {summary['partials']} partials",
                payload=settle_res,
            )

            # Learning loop on each closed trade
            for ev in settle_res.get("settled", []):
                t = get_trade(ev["trade_id"])
                if t and t.get("status") == "CLOSED":
                    try:
                        learn_from_trade_outcome(t)
                    except Exception as e:
                        log.error(f"learning failed for trade {t['id']}: {e}")
        except Exception as e:
            log_scheduler_event("ERROR",
                                f"Auto-settle failed: {e}", "ERROR")

    # ---- Auto-entry on best GOLD BUYs (only if enabled) ----
    if autotrade and not df.empty:
        # Robo-only entry window: don't open trades in the last hour
        # (no time for the position to work before close).
        now = get_myt_now()
        entry_cutoff = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now > entry_cutoff:
            log_scheduler_event(
                "AUTO_ENTRY_SKIP",
                f"Inside robo no-entry window (after 16:00 MYT)", "INFO")
            return summary
        from repository import load_trades
        log_scheduler_event("AUTO_ENTRY_START", "Evaluating new entries")
        trades = load_trades()
        active = active_trades()
        active_tickers = {t["ticker"] for t in active}
        acc = load_account()
        cash = acc["cash_balance"]
        threshold = regime.get("position_rules", {}).get("new_signal_threshold", 0.70) * 100

        gold_buys = df[df["signal"].astype(str).str.contains("GOLD BUY", regex=False)]
        gold_buys = gold_buys[gold_buys["confidence"] >= threshold]
        gold_buys = gold_buys.head(regime.get("position_rules", {})
                                   .get("max_concurrent_positions", 5))

        risk_per_trade_rm = 0.01 * acc["initial_capital"]   # 1 % of capital

        for _, row in gold_buys.iterrows():
            if row["ticker"] in active_tickers:
                continue
            entry = row["entry"]; sl = row["stop_loss"]
            risk_per_share = max(entry - sl, 0.001)
            target_shares = int(risk_per_trade_rm / risk_per_share)
            target_shares = (target_shares // 100) * 100
            if target_shares < 100:
                continue
            actual_cost = target_shares * entry
            risk_check = run_full_risk_check(
                trades, {"ticker": row["ticker"], "sector": row["sector"],
                         "entry": entry, "stop_loss": sl,
                         "cost": actual_cost,
                         "risk_amount": risk_per_share * target_shares},
                cash, acc["initial_capital"])
            if not risk_check["pass"]:
                summary["rejected"] += 1
                from logger import log_trade_event
                log_trade_event(
                    "RISK_REJECTED", trade_id=None, ticker=row["ticker"],
                    actor="AGENT",
                    payload={"verdict": risk_check["final_verdict"]})
                # v3.1: optional alert on rejection (off by default)
                try:
                    from live_trigger import fire as _live_fire
                    _live_fire("RISK_REJECTED", trade_id=None,
                               ticker=row["ticker"], actor="AGENT",
                               payload={"verdict": risk_check["final_verdict"]})
                except Exception:
                    pass
                continue
            sized_shares = int(target_shares * risk_check["size_multiplier"])
            sized_shares = (sized_shares // 100) * 100
            if sized_shares < 100:
                continue
            try:
                ok, tid, msg = execute_entry(
                    row["ticker"], row["name"], row["sector"],
                    entry, sl, row["tp1"], row["tp2"], row["tp3"],
                    row["signal"], sized_shares,
                    {"reasoning": row.get("reasoning", ""),
                     "rsi": row.get("rsi"), "vol_ratio": row.get("vol_ratio"),
                     "atr": row.get("atr"), "support": row.get("support"),
                     "resistance": row.get("resistance"),
                     "macd_hist": row.get("macd_hist"),
                     "ema_trend": row.get("ema_trend", entry)},
                    regime, row["confidence"],
                    execution_type="AUTO", actor="AGENT")
                if ok:
                    summary["auto_entries"] += 1
                    cash -= sized_shares * entry * 1.0015
            except Exception as e:
                summary["errors"].append(f"{row['ticker']}: {e}")

        log_scheduler_event("AUTO_ENTRY_END",
                            f"{summary['auto_entries']} entries, "
                            f"{summary['rejected']} rejected")

    return summary


# -------------------------------------------------------------------------
# Loop
# -------------------------------------------------------------------------

def _loop(interval_sec: int):
    """The actual background loop. Exits when _STOP_EVENT is set."""
    update_scheduler_state(
        running=1,
        last_heartbeat=myt_iso(),
        next_run_at=myt_iso(_next_run_at(interval_sec)),
        last_error="",
    )
    log_scheduler_event("STARTED", f"Robo-Trader started (interval {interval_sec}s)")

    while not _STOP_EVENT.is_set():
        state = get_scheduler_state()
        if state.get("kill_switch", 0):
            log_scheduler_event("KILLED", "kill_switch=1 — exiting loop", "WARN")
            break

        # HEARTBEAT — always update next_run_at on every wake-up
        # so the UI shows the correct upcoming sleep target, regardless
        # of whether this wake-up actually ran a cycle.
        next_at = myt_iso(_next_run_at(interval_sec))
        update_scheduler_state(
            last_heartbeat=myt_iso(),
            next_run_at=next_at,
        )
        log_scheduler_event("HEARTBEAT", "alive")

        # Check market hours
        if not _is_market_hours():
            from risk_manager import check_trading_time_window
            tw = check_trading_time_window()
            log_scheduler_event(
                "SKIP",
                f"Outside market hours — {tw.get('reason','closed')}. "
                f"Sleeping until {next_at}",
            )
        else:
            try:
                autotrade = bool(state.get("autotrade_enabled", 0))
                autoexit = bool(state.get("autoexit_enabled", 1))
                t0 = time.time()
                summary = _run_one_cycle(autotrade=autotrade, autoexit=autoexit)
                duration = time.time() - t0
                update_scheduler_state(
                    last_run_at=myt_iso(),
                    next_run_at=myt_iso(_next_run_at(interval_sec)),
                    consecutive_failures=0,
                    last_error="",
                )
                log_scheduler_event(
                    "CYCLE_OK",
                    f"scan={summary['scan_count']} settled={summary['settled']} "
                    f"partials={summary['partials']} entries={summary['auto_entries']}",
                    duration_sec=duration, payload=summary,
                )
            except Exception as e:
                tb = traceback.format_exc()
                fails = (state.get("consecutive_failures") or 0) + 1
                update_scheduler_state(consecutive_failures=fails,
                                       last_error=f"{e}\n{tb}")
                log_scheduler_event("CYCLE_ERROR", str(e), "ERROR",
                                    payload={"trace": tb})

        # Daily maintenance window (best-effort, idempotent)
        try:
            now = get_myt_now()
            if now.hour == 1 and now.minute < 5:
                # 1. Prune logs to bounded size
                prune_logs(5000)
                # 2. Re-train the ML setup classifier with latest data
                try:
                    from learner import train_setup_classifier
                    res = train_setup_classifier()
                    if res:
                        log_scheduler_event(
                            "NIGHTLY_RETRAIN",
                            f"ML classifier retrained, OOS acc={res[1]:.3f}")
                except Exception as e:
                    log_scheduler_event(
                        "NIGHTLY_RETRAIN", f"failed: {e}", "ERROR")
            # 3. Auto-disable exploration once target trades reached
            from repository import closed_trades
            ss = get_scheduler_state()
            if ss.get("exploration_mode"):
                tgt = ss.get("exploration_trades_target", 50) or 50
                done = len(closed_trades())
                if done >= tgt:
                    update_scheduler_state(exploration_mode=0)
                    log_scheduler_event(
                        "EXPLORATION_END",
                        f"Closed {done} trades — switching to "
                        "exploitation (LCB) mode", "INFO")
        except Exception:
            pass

        # Sleep until next scheduled time, but allow early wake on stop
        nxt = _next_run_at(interval_sec)
        sleep_secs = max(60, (nxt - get_myt_now()).total_seconds())
        _STOP_EVENT.wait(timeout=sleep_secs)

    update_scheduler_state(running=0,
                           last_heartbeat=myt_iso())
    log_scheduler_event("STOPPED", "Robo-Trader loop exited")


# -------------------------------------------------------------------------
# Public control
# -------------------------------------------------------------------------

def start(interval_sec: int = 3600) -> bool:
    """Start the background thread if not already running."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP_EVENT.clear()
        # Set running=1 BEFORE spawning so is_running() is correct immediately.
        update_scheduler_state(
            interval_sec=interval_sec, kill_switch=0, running=1,
            last_heartbeat=myt_iso(),
        )
        _THREAD = threading.Thread(
            target=_loop, args=(interval_sec,),
            name="bursa-scheduler", daemon=True,
        )
        _THREAD.start()
        return True


def stop() -> None:
    """Request the thread to exit."""
    global _THREAD
    with _LOCK:
        _STOP_EVENT.set()
        update_scheduler_state(kill_switch=1)
        if _THREAD is not None:
            _THREAD.join(timeout=5)
        _THREAD = None


def is_running() -> bool:
    with _LOCK:
        alive = _THREAD is not None and _THREAD.is_alive()
    state = get_scheduler_state()
    return alive and bool(state.get("running", 0))


def force_restart(interval_sec: int = 3600) -> None:
    """User-facing 'Force Reboot Robo-Trader'."""
    stop()
    time.sleep(0.5)
    start(interval_sec=interval_sec)


def ensure_started(interval_sec: int = 3600,
                   max_heartbeat_age_sec: int | None = None) -> None:
    """
    Idempotent + self-healing.

    1. If not running → start.
    2. If running but heartbeat is older than 2× interval → force-restart.

    Streamlit re-runs the script on every interaction; daemon threads
    usually survive but can die silently if the parent process is restarted
    without killing children. The heartbeat check is the safety net.
    """
    if not is_running():
        start(interval_sec=interval_sec)
        return
    # Self-heal: stale heartbeat → restart
    state = get_scheduler_state()
    hb = state.get("last_heartbeat")
    if not hb:
        return  # never beat yet, give it time
    try:
        hb_dt = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8)))
        age = (get_myt_now() - hb_dt).total_seconds()
    except Exception:
        return
    threshold = max_heartbeat_age_sec or (interval_sec * 2 + 120)
    if age > threshold:
        log_scheduler_event(
            "SELF_HEAL",
            f"Heartbeat stale ({age:.0f}s > {threshold}s) — force-restarting",
            "WARN",
        )
        force_restart(interval_sec=interval_sec)


def run_once() -> dict:
    """Trigger a single immediate cycle (used by 'Run now' button)."""
    state = get_scheduler_state()
    return _run_one_cycle(
        autotrade=bool(state.get("autotrade_enabled", 0)),
        autoexit=bool(state.get("autoexit_enabled", 1)),
    )


# -------------------------------------------------------------------------
# CLI entry — `python -m scheduler`
# -------------------------------------------------------------------------

if __name__ == "__main__":
    interval = 3600
    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        try:
            interval = int(sys.argv[idx + 1])
        except Exception:
            pass

    start(interval_sec=interval)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        stop()
