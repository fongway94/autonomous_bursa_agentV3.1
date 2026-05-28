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


def _explain_cycle_outcome(summary: dict, df, regime: dict,
                            threshold: float, active_count: int,
                            max_positions: int,
                            autotrade_enabled: bool) -> str:
    """
    Build a human-readable explanation of why a cycle ended with zero
    new entries. Returns a short sentence ready to log.

    Order of checks matches the actual cycle flow so the reason
    reported is the FIRST blocking condition encountered.
    """
    regime_name = (regime or {}).get("regime_data", {}).get("regime", "?")
    conv = (regime or {}).get("position_rules", {}).get("conviction_pct", 0)

    # 1. Auto-trade disabled
    if not autotrade_enabled:
        return ("Auto-entry is OFF. Toggle it on in 🤖 Robo-Trader tab "
                "to let the agent open positions.")

    # 2. Scan returned nothing
    if df is None or len(df) == 0:
        return ("Scanner returned 0 results — likely a yfinance data outage. "
                "Check 📜 Logs → Data Quality.")

    # 3. Count GOLD BUYs at all
    gold_buys_all = df[df["signal"].astype(str).str.contains(
        "GOLD BUY", regex=False)]
    if len(gold_buys_all) == 0:
        return (f"No GOLD BUY signals found across {len(df)} scanned stocks "
                f"in {regime_name} regime. Market may be in distribution / "
                "no breakout or pullback setups today.")

    # 4. Above-threshold qualifiers
    qualifiers = gold_buys_all[gold_buys_all["confidence"] >= threshold]
    if len(qualifiers) == 0:
        best_conf = float(gold_buys_all["confidence"].max())

        # v3.1.4: include regime trend so user knows if BEAR is weakening
        # (entries may resume soon) or strengthening (still wait).
        trend_note = ""
        try:
            from repository import get_regime_trend
            trend = get_regime_trend(lookback_hours=24)
            if trend["samples"] >= 2:
                # Direction word depends on regime — strengthening BEAR
                # is bad for entries; strengthening BULL is good.
                if regime_name == "BEAR":
                    if trend["direction"] == "WEAKENING":
                        feel = "weakening (good — entries may resume soon)"
                    elif trend["direction"] == "STRENGTHENING":
                        feel = "strengthening (BEAR getting deeper)"
                    else:
                        feel = "stable"
                elif regime_name == "BULL":
                    feel = trend["direction"].lower()
                else:
                    feel = trend["direction"].lower()

                trend_note = (
                    f" Regime conviction: {trend['current_conviction']:.0f}% "
                    f"(was {trend['avg_recent_conviction']:.0f}% avg over last "
                    f"24h → {feel})."
                )
                # Add the actionable signal — KLCI distance from 200-EMA
                if trend["ema_200_distance_pct"] is not None:
                    dist = trend["ema_200_distance_pct"]
                    if dist < 0:
                        trend_note += (
                            f" KLCI is {abs(dist):.1f}% BELOW its 200-EMA "
                            "— watch for it to cross ABOVE for regime flip."
                        )
                    else:
                        trend_note += (
                            f" KLCI is {dist:.1f}% above its 200-EMA — "
                            "regime should normalize as trend confirms."
                        )
        except Exception:
            pass  # trend is a nice-to-have, never block the explanation

        return (f"{len(gold_buys_all)} GOLD BUY signal(s) found, but the "
                f"highest confidence was {best_conf:.0f}/100 — below the "
                f"{regime_name} regime threshold of {threshold:.0f}. "
                f"Agent stays defensive.{trend_note}")

    # 5. Position cap
    if active_count >= max_positions:
        return (f"At max concurrent positions ({active_count}/{max_positions} "
                f"in {regime_name} regime). Wait for existing positions to "
                "close before opening new ones.")

    # 6. All qualifiers already held
    active_tickers = set()
    try:
        from repository import active_trades
        active_tickers = {t["ticker"] for t in active_trades()}
    except Exception:
        pass
    new_qualifiers = qualifiers[~qualifiers["ticker"].isin(active_tickers)]
    if len(new_qualifiers) == 0:
        return (f"{len(qualifiers)} qualifying GOLD BUY signal(s), but all "
                "are already in active positions. No new entries possible.")

    # 7. Some new qualifiers existed but all rejected by risk check
    if summary.get("rejected", 0) > 0:
        return (f"{summary['rejected']} qualifying signal(s) rejected by "
                "risk checks (drawdown / position limit / sector cap / "
                "daily limit). See 📜 Logs → Trade executions for details.")

    # 8. Catch-all — shouldn't normally hit this branch
    return (f"{len(new_qualifiers)} candidate(s) existed but no entries "
            "fired (lot-size rounding may have reduced shares below 100).")


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
        # v3.1.4: record regime snapshot for trend analysis
        try:
            from repository import record_regime_snapshot
            rd = regime.get("regime_data", {})
            details = rd.get("details", {})
            record_regime_snapshot(
                regime=rd.get("regime", "UNKNOWN"),
                conviction=rd.get("conviction", 0),
                trend_score=details.get("trend_score"),
                ema_200_vs_price=details.get("ema_200_vs_price"),
                klci_rsi=details.get("klci_rsi"),
            )
        except Exception:
            pass  # never block the cycle on history recording
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
            # v3.1.5: backup immediately after any closed trades —
            # preserves brain learning + new account balance
            if settle_res.get("settled"):
                try:
                    from persistence import backup as _pers_backup, is_configured
                    if is_configured():
                        _pers_backup(reason=f"{len(settle_res['settled'])} trade(s) closed")
                except Exception:
                    pass
        except Exception as e:
            log_scheduler_event("ERROR",
                                f"Auto-settle failed: {e}", "ERROR")

    # ---- Auto-entry on best GOLD BUYs (only if enabled) ----
    if autotrade and not df.empty:
        # Robo-only entry window: use the calendar's safe-entry rule
        # (09:00-12:30 morning + 14:30-16:00 afternoon). Blocks new
        # entries in lunch break and in the last hour before close.
        from market_calendar import is_safe_entry_window, current_session
        if not is_safe_entry_window():
            sess = current_session()
            sess_name = sess.name if sess else "outside-session"
            from market_calendar import market_status_text
            ms = market_status_text()
            log_scheduler_event(
                "AUTO_ENTRY_SKIP",
                f"0 entries — In {sess_name} session "
                f"({ms.get('reason', '')}). "
                "New auto-entries only fire 09:00-12:30 and 14:30-16:00 MYT. "
                f"Next opportunity: {ms.get('next_event', '?')}",
                "INFO",
                payload={"reason": "outside_safe_entry_window",
                         "session": sess_name})
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

        # Append a human-readable reason when nothing got opened
        if summary["auto_entries"] == 0:
            reason = _explain_cycle_outcome(
                summary, df, regime, threshold,
                active_count=len(active),
                max_positions=regime.get("position_rules", {})
                                   .get("max_concurrent_positions", 5),
                autotrade_enabled=True,   # we're inside the autotrade block
            )
            log_scheduler_event(
                "AUTO_ENTRY_END",
                f"0 entries — {reason}",
                payload={"reason": reason,
                         "regime": regime.get("regime_data", {}).get("regime"),
                         "threshold": threshold,
                         "scan_count": summary["scan_count"]})
        else:
            log_scheduler_event(
                "AUTO_ENTRY_END",
                f"{summary['auto_entries']} entries, "
                f"{summary['rejected']} rejected")

    # If we get here without entering the autotrade block, autotrade is OFF
    # OR df is empty. Make sure the cycle log still explains it.
    elif autotrade:
        log_scheduler_event(
            "AUTO_ENTRY_END",
            "0 entries — Scanner returned 0 results. Likely yfinance "
            "outage; check 📜 Logs → Data Quality.",
            payload={"reason": "empty_scan"})
    else:
        log_scheduler_event(
            "AUTO_ENTRY_END",
            "0 entries — Auto-entry is OFF. Toggle it on in "
            "🤖 Robo-Trader tab to let the agent open positions.",
            payload={"reason": "autotrade_disabled"})

    return summary


# -------------------------------------------------------------------------
# Loop
# -------------------------------------------------------------------------

def _loop(interval_sec: int, my_pid: int):
    """The actual background loop. Exits when _STOP_EVENT is set,
    kill_switch is engaged, or a newer process has claimed ownership.

    DEBOUNCE: On fresh startup (typical after a GitHub push triggers a
    Streamlit Cloud redeploy), we DO NOT run a cycle immediately.
    Instead we sleep until the next scheduled boundary. This prevents
    multiple pushes within an hour from each triggering a full market
    scan — which is wasteful (yfinance hits) and confusing in the logs.

    The user can still force an immediate cycle via the "⚡ Run Cycle Now"
    button in the Robo-Trader tab (it calls run_once() directly, bypassing
    this debounce).
    """
    next_boundary = _next_run_at(interval_sec)
    update_scheduler_state(
        running=1,
        last_heartbeat=myt_iso(),
        next_run_at=myt_iso(next_boundary),
        last_error="",
        owner_pid=my_pid,
    )
    log_scheduler_event(
        "STARTED",
        f"Robo-Trader started (PID {my_pid}, interval {interval_sec}s). "
        f"First cycle deferred to next boundary: "
        f"{next_boundary.strftime('%Y-%m-%d %H:%M:%S')} MYT.",
    )

    # Debounce: sleep until the next scheduled boundary before the first
    # cycle. Allows early wake on stop event.
    delay = max(0, (next_boundary - get_myt_now()).total_seconds())
    if delay > 0:
        _STOP_EVENT.wait(timeout=delay)

    while not _STOP_EVENT.is_set():
        state = get_scheduler_state()
        if state.get("kill_switch", 0):
            log_scheduler_event("KILLED", "kill_switch=1 — exiting loop", "WARN")
            break
        # Ghost-thread eviction (v3.1.8 hardened): check ownership FIRST
        # and exit silently — no log spam — if another live owner exists.
        current_owner = state.get("owner_pid", 0) or 0
        if current_owner and current_owner != my_pid:
            # Is the current owner still alive? Check their heartbeat freshness.
            other_hb = state.get("last_heartbeat")
            other_is_alive = False
            age_sec = None
            if other_hb:
                try:
                    other_dt = datetime.strptime(other_hb, "%Y-%m-%d %H:%M:%S")
                    other_dt = other_dt.replace(tzinfo=timezone(timedelta(hours=8)))
                    age_sec = (get_myt_now() - other_dt).total_seconds()
                    # 5-minute window: if they beat within last 5min, they're alive
                    other_is_alive = (age_sec < 300)
                except Exception:
                    pass
            if other_is_alive:
                # Live owner exists — exit SILENTLY (no log, no work).
                # Only log ONCE per process to leave a breadcrumb.
                if not getattr(_loop, "_silent_exit_logged", False):
                    log_scheduler_event(
                        "GHOST_EXIT",
                        f"PID {my_pid} exiting — owner PID {current_owner} "
                        f"alive (last beat {age_sec:.0f}s ago)", "WARN")
                    setattr(_loop, "_silent_exit_logged", True)
                break
            # Otherwise, the previous owner appears dead — take over silently.
            log_scheduler_event(
                "OWNER_TAKEOVER",
                f"PID {my_pid} taking over from stale owner "
                f"PID {current_owner} (no beat for "
                f"{age_sec if age_sec is not None else '?'}s)", "WARN")

        # HEARTBEAT — always update next_run_at on every wake-up
        # so the UI shows the correct upcoming sleep target, regardless
        # of whether this wake-up actually ran a cycle. Re-asserts our
        # owner_pid to fend off any racing ghost thread.
        next_at = myt_iso(_next_run_at(interval_sec))
        update_scheduler_state(
            last_heartbeat=myt_iso(),
            next_run_at=next_at,
            owner_pid=my_pid,
        )
        log_scheduler_event("HEARTBEAT", f"alive (PID {my_pid})")

        # v3.1.5: hourly DB backup safety net (only if GITHUB_TOKEN configured)
        try:
            from persistence import backup as _pers_backup, is_configured
            if is_configured():
                res = _pers_backup(reason="hourly heartbeat")
                if not res.get("ok") and not res.get("skipped"):
                    log_scheduler_event(
                        "BACKUP_FAIL",
                        f"Hourly backup failed: {res.get('reason','?')}",
                        "WARN")
        except Exception:
            pass

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
                autotrade = bool(state.get("autotrade_enabled", 1))  # v3 default ON
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

        # ---- Daily maintenance window (TRULY idempotent, v3.1.1) ----
        # Two layers of protection against duplicate runs:
        #   1. Re-check owner_pid right before maintenance — ghost threads
        #      that took over mid-iteration get evicted here too.
        #   2. try_claim_daily_task() does an atomic SQL CAS — only one
        #      caller (across any number of ghost threads or sibling
        #      processes) can claim each task per MYT calendar day.
        try:
            now = get_myt_now()
            in_maintenance_window = (now.hour == 1 and now.minute < 5)

            # Layer 1: re-verify ownership before any maintenance work
            current_owner = get_scheduler_state().get("owner_pid", 0) or 0
            if current_owner and current_owner != my_pid:
                pass  # not our turn — skip maintenance entirely
            elif in_maintenance_window:
                from repository import (
                    try_claim_daily_task, record_daily_task_result,
                )

                # 1. Log pruning (claim before doing work)
                if try_claim_daily_task("prune_logs", my_pid):
                    prune_logs(5000)
                    record_daily_task_result("prune_logs", "ok")

                # 2. ML classifier nightly retrain — THIS is the one that
                #    was firing 8× per night before the idempotency guard.
                if try_claim_daily_task("ml_retrain", my_pid):
                    try:
                        from learner import train_setup_classifier
                        res = train_setup_classifier()
                        if res:
                            log_scheduler_event(
                                "NIGHTLY_RETRAIN",
                                f"ML classifier retrained, OOS acc={res[1]:.3f} "
                                f"(PID {my_pid})")
                            record_daily_task_result(
                                "ml_retrain", f"oos_acc={res[1]:.4f}")
                        else:
                            record_daily_task_result(
                                "ml_retrain", "no_result")
                    except Exception as e:
                        log_scheduler_event(
                            "NIGHTLY_RETRAIN", f"failed: {e}", "ERROR")
                        record_daily_task_result(
                            "ml_retrain", f"error: {e}")

            # 3. Exploration auto-disable — also idempotent, but checked
            #    every iteration (not just during maintenance window) so
            #    the switch can happen the moment the threshold is hit.
            from repository import closed_trades
            ss = get_scheduler_state()
            if ss.get("exploration_mode"):
                tgt = ss.get("exploration_trades_target", 50) or 50
                done = len(closed_trades())
                if done >= tgt:
                    # CAS update: only one writer flips the flag and logs.
                    if try_claim_daily_task("exploration_end", my_pid):
                        update_scheduler_state(exploration_mode=0)
                        log_scheduler_event(
                            "EXPLORATION_END",
                            f"Closed {done} trades — switching to "
                            "exploitation (LCB) mode", "INFO")
                        record_daily_task_result(
                            "exploration_end", f"flipped_at_{done}_trades")
        except Exception as e:
            log_scheduler_event(
                "MAINTENANCE_ERROR", f"{e}", "ERROR")

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
    """
    Start the background thread if not already running.

    Sets owner_pid to our process ID. Any older ghost loop (from a
    previous Streamlit Cloud deploy) will detect the PID change on
    its next iteration and exit cleanly.
    """
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP_EVENT.clear()
        my_pid = os.getpid()
        # v3.1.8: Harden against same-process duplicate threads.
        # The module-level _THREAD handle can be reset to None during
        # Streamlit module reloads or stop()/join(timeout) races while
        # the actual daemon thread is still alive.  We now enforce
        # single-process uniqueness via three layers:
        #   1) _THREAD handle (fast, local)
        #   2) threading.enumerate() scan for any alive "bursa-scheduler"
        #   3) DB heartbeat freshness for this PID
        for t in threading.enumerate():
            if getattr(t, "name", None) == "bursa-scheduler" and t.is_alive():
                return False
        state = get_scheduler_state()
        if (state.get("running") == 1 and state.get("owner_pid") == my_pid):
            hb = state.get("last_heartbeat")
            if hb:
                try:
                    hb_dt = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone(timedelta(hours=8)))
                    age = (get_myt_now() - hb_dt).total_seconds()
                    if age < 300:
                        return False
                except Exception:
                    pass
        # Detect + log if a stale owner is being evicted (likely a
        # ghost thread from a previous Streamlit Cloud deploy).
        prev = state
        prev_pid = prev.get("owner_pid", 0) or 0
        if prev_pid and prev_pid != my_pid:
            log_scheduler_event(
                "EVICT_GHOST",
                f"Detected previous owner PID {prev_pid}; claiming as "
                f"PID {my_pid}. Old loop will self-terminate on next wake.",
                "WARN",
            )
        # Set running=1 + owner_pid BEFORE spawning so is_running() is
        # correct immediately and ghost loops detect the takeover.
        update_scheduler_state(
            interval_sec=interval_sec, kill_switch=0, running=1,
            owner_pid=my_pid,
            last_heartbeat=myt_iso(),
        )
        _THREAD = threading.Thread(
            target=_loop, args=(interval_sec, my_pid),
            name="bursa-scheduler", daemon=True,
        )
        _THREAD.start()
        return True


def stop() -> None:
    """Request the thread to exit."""
    global _THREAD
    with _LOCK:
        _STOP_EVENT.set()
        # v3.1.8: also set running=0 so that start() / ensure_started()
        # can immediately respawn after an intentional stop, rather than
        # being blocked by the DB-based same-process guard.
        update_scheduler_state(kill_switch=1, running=0)
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

    v3.1.8: be conservative — don't spawn duplicate loops.
    If another process is the owner and beat recently, do NOTHING.

    1. If another live owner exists → do nothing.
    2. If we own it and our local thread is alive → do nothing.
    3. If stale/no owner → start.
    4. If our local thread is a ghost (DB shows other owner) → force-restart.

    Streamlit re-runs the script on every interaction; daemon threads
    usually survive but can die silently if the parent process is restarted
    without killing children. The heartbeat check is the safety net.
    """
    state = get_scheduler_state()
    current_owner = state.get("owner_pid", 0) or 0
    hb = state.get("last_heartbeat")
    my_pid = os.getpid()

    if hb:
        try:
            hb_dt = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8)))
            age = (get_myt_now() - hb_dt).total_seconds()
        except Exception:
            age = None
    else:
        age = None

    # Case 1: another process owns it and beat in the last 5 min → don't touch.
    if current_owner and current_owner != my_pid and age is not None and age < 300:
        return  # another live worker is handling this

    # Case 2: we own it and our local thread is alive → all good.
    if current_owner == my_pid and is_running():
        return

    # v3.1.8: DB indicates a live same-process owner but _THREAD handle
    # was lost (Streamlit module reload, stop/join race, etc.).  start()
    # will reject too, but short-circuit here to avoid log noise.
    if current_owner == my_pid and not is_running():
        if bool(state.get("running", 0)) and age is not None and age < 300:
            return

    # Case 3: stale or no owner — we should take over.
    if not is_running():
        start(interval_sec=interval_sec)
        return

    # Case 4: thread alive locally but DB shows another owner — defer to DB.
    # This means our local thread is a stale ghost from a previous Streamlit
    # run. Stop it and let the rightful owner continue.
    threshold = max_heartbeat_age_sec or (interval_sec * 2 + 120)
    if age is not None and age > threshold:
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
        autotrade=bool(state.get("autotrade_enabled", 1)),  # v3 default ON
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
