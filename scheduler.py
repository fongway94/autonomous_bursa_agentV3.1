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

v3.1.10 — Zombie thread recovery + cycle watchdog
-------------------------------------------------
Earlier guards in v3.1.8/3.1.9 were over-conservative: when a loop
got stuck inside a long network call (e.g. yfinance hang) or inside a
multi-second `_STOP_EVENT.wait()` past the 5-second stop() join window,
the thread would survive a Stop / Kill-Switch click. On the next
Start click, `start()` Guard 2 would enumerate threads, find the still-
alive zombie, ADOPT it, and return False — leaving the UI permanently
on "🔴 STOPPED" with no path back to RUNNING.

Two complementary mechanisms now defend against this:

  A. ``_ORPHANED_THREAD_IDS`` registry (UI-recovery path)
     * ``stop()`` records the dying thread's ``ident`` here (regardless
       of whether the bounded join() succeeded).
     * ``start()`` Guard 2 IGNORES any "bursa-scheduler" thread whose
       ident is in this set — they are zombies en route to self-
       termination via the existing owner_pid eviction inside ``_loop``.
     * ``force_restart()`` no longer blocks for 30 s waiting for the
       zombie to die. The zombie will self-cull on its next wake-up.

  B. ``_watchdog_loop`` (autonomous-recovery path)
     * Separate lightweight thread that wakes every 60 s.
     * Reads ``scheduler_state.cycle_started_at``. If a cycle has been
       running > ``WATCHDOG_CYCLE_TIMEOUT_SEC`` (default 600 = 10 min),
       it (1) logs ``CYCLE_TIMEOUT``, (2) clears cycle_started_at,
       (3) bumps owner_pid to a synthetic value so the stuck loop self-
       exits on its next wake, (4) marks the stuck thread as orphaned.
     * Next scheduled boundary spawns a fresh ``_loop`` via
       ``ensure_started`` from the Streamlit UI rerun.

The existing ``_loop`` ownership check (compares its ``my_pid`` against
``scheduler_state.owner_pid`` on every iteration) guarantees the orphan
exits cleanly the moment it next wakes up; we do not leak threads.

Python note: ``threading`` cannot forcibly interrupt blocking I/O — that
requires OS signals which are main-thread-only. The watchdog can only
make the system *recover* within N minutes; it cannot make the stuck
cycle return faster. That's why each yfinance / HTTP call must have its
own timeout (verified in screener.py, market_analyzer.py, learner.py,
persistence.py, notifier.py, evaluation.py).
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

# v3.1.10: registry of thread idents that stop() has asked to exit but
# that may still be alive (stuck in a sleep / blocking I/O). Guard 2
# in start() must skip these — adopting a dying thread is what caused
# the "permanently STOPPED" UI bug.
_ORPHANED_THREAD_IDS: set[int] = set()

# v3.1.10: watchdog thread — separate from the scheduler loop. Detects
# runaway cycles (e.g. yfinance/network hang) and forces a clean handoff
# so the scheduler recovers autonomously instead of waiting for a human
# to click Force Restart.
_WATCHDOG_THREAD: threading.Thread | None = None
_WATCHDOG_STOP_EVENT = threading.Event()

# Tunable knobs (kept here, not in DB — these are deploy-time config
# rather than user-facing settings).
WATCHDOG_TICK_SEC = 60                  # how often the watchdog checks
WATCHDOG_CYCLE_TIMEOUT_SEC = 600        # 10 min — cycle is "runaway"
WATCHDOG_TIMEOUT_OWNER_SENTINEL = -1    # forces owner_pid mismatch
CYCLE_DURATION_WARN_SEC = 300           # 5 min — soft warn, no action


def _gc_orphaned_thread_ids() -> None:
    """Remove idents from the orphan set whose thread has actually died."""
    if not _ORPHANED_THREAD_IDS:
        return
    alive_idents = {t.ident for t in threading.enumerate() if t.is_alive()}
    _ORPHANED_THREAD_IDS.intersection_update(alive_idents)


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

def _run_one_cycle(autotrade: bool, autoexit: bool,
                     my_pid: int | None = None) -> dict:
    """One full scan + settle cycle. Returns summary dict."""
    # v3.1.9: ownership check at the very top — if another thread has
    # claimed the scheduler while we were mid-cycle, abort immediately.
    if my_pid is not None:
        state = get_scheduler_state()
        current_owner = state.get("owner_pid", 0) or 0
        if current_owner and current_owner != my_pid:
            log_scheduler_event(
                "CYCLE_ABORT",
                f"PID {my_pid} aborting cycle — owner_pid now {current_owner}",
                "WARN",
            )
            return {"scan_count": 0, "settled": 0, "partials": 0,
                    "auto_entries": 0, "rejected": 0, "errors": [],
                    "aborted": True}

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
# Watchdog
# -------------------------------------------------------------------------

def _watchdog_loop(my_pid: int):
    """
    Wakes every WATCHDOG_TICK_SEC. If a cycle has been running longer than
    WATCHDOG_CYCLE_TIMEOUT_SEC, forces a clean handoff:

      1. Log CYCLE_TIMEOUT with the stuck duration
      2. Reset cycle_started_at to NULL (so we don't re-fire next tick)
      3. Bump owner_pid to WATCHDOG_TIMEOUT_OWNER_SENTINEL so the stuck
         loop self-exits on its next wake via the existing
         owner_pid-mismatch check inside _loop.
      4. Mark the stuck thread as orphaned so the next start() ignores it.

    Note: Python ``threading`` cannot interrupt blocking I/O. The
    watchdog cannot make the stuck cycle return any faster than its
    underlying network timeouts allow — it only ensures the system
    *recovers* within WATCHDOG_CYCLE_TIMEOUT_SEC instead of forever.

    This is why per-call HTTP timeouts (yfinance ``timeout=15``,
    requests ``timeout=30``, smtp ``timeout=...``) are non-negotiable.
    The watchdog is the second line of defence, not the first.
    """
    log_scheduler_event(
        "WATCHDOG_STARTED",
        f"Watchdog started (PID {my_pid}, "
        f"timeout={WATCHDOG_CYCLE_TIMEOUT_SEC}s, tick={WATCHDOG_TICK_SEC}s)",
        "INFO",
    )
    while not _WATCHDOG_STOP_EVENT.is_set():
        try:
            state = get_scheduler_state()
            cycle_started = state.get("cycle_started_at")
            scheduler_owner = state.get("owner_pid", 0) or 0

            # Only act if our PID still owns the scheduler — otherwise
            # another process is in charge and its own watchdog should
            # handle it. Avoids cross-process false positives.
            if cycle_started and scheduler_owner == my_pid:
                try:
                    started_dt = datetime.strptime(
                        cycle_started, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone(timedelta(hours=8)))
                    age = (get_myt_now() - started_dt).total_seconds()
                except Exception:
                    age = None

                if age is not None and age > WATCHDOG_CYCLE_TIMEOUT_SEC:
                    # Runaway cycle detected — force handoff.
                    log_scheduler_event(
                        "CYCLE_TIMEOUT",
                        f"Watchdog: cycle has been running {age:.0f}s "
                        f"(> {WATCHDOG_CYCLE_TIMEOUT_SEC}s limit). "
                        f"Forcing handoff — the stuck loop will self-exit "
                        f"on next wake.",
                        "ERROR",
                        payload={"stuck_duration_sec": age,
                                 "owner_pid": scheduler_owner},
                    )
                    # Clear cycle_started_at + bump owner_pid in one write.
                    # Setting last_error gives the user something visible
                    # in the Robo-Trader tab.
                    update_scheduler_state(
                        cycle_started_at=None,
                        owner_pid=WATCHDOG_TIMEOUT_OWNER_SENTINEL,
                        running=0,
                        last_error=(
                            f"Cycle exceeded {WATCHDOG_CYCLE_TIMEOUT_SEC}s "
                            f"(ran {age:.0f}s). Watchdog forced handoff. "
                            "Likely a yfinance / network hang."
                        ),
                    )
                    # Mark the stuck scheduler thread as orphaned so
                    # start()'s Guard 2 ignores it on the next click /
                    # Streamlit rerun. We don't know its exact ident from
                    # here without _LOCK, so iterate threading.enumerate.
                    try:
                        with _LOCK:
                            if (_THREAD is not None
                                    and _THREAD.is_alive()
                                    and _THREAD.ident is not None):
                                _ORPHANED_THREAD_IDS.add(_THREAD.ident)
                    except Exception:
                        pass
        except Exception as e:
            # The watchdog itself must never crash the process.
            try:
                log_scheduler_event(
                    "WATCHDOG_ERROR", f"{e}", "ERROR")
            except Exception:
                pass

        _WATCHDOG_STOP_EVENT.wait(timeout=WATCHDOG_TICK_SEC)


def _ensure_watchdog_running(my_pid: int) -> None:
    """
    Idempotent watchdog spawner. Safe to call from every code path that
    "knows the scheduler should be alive" — including:

      * end of a successful ``start()`` (initial spawn)
      * the ADOPT_THREAD branch of ``start()`` (we adopted an existing
        scheduler thread but the watchdog may have died)
      * ``ensure_started()`` Case 2 (we own a healthy scheduler — this is
        the Streamlit Cloud "every rerun" path)

    v3.1.11: this REPLACES the old ``_start_watchdog`` which was only
    called from one place. The user reported seeing ADOPT_THREAD events
    in their live scheduler log but ZERO WATCHDOG_STARTED events,
    proving the old gating was too narrow.

    Logs ``WATCHDOG_STARTED`` on first spawn per session, then
    ``WATCHDOG_RESPAWN`` for any subsequent respawn so ops can see
    "the watchdog keeps dying" in the logs.
    """
    global _WATCHDOG_THREAD
    # Already running? Skip silently — no log spam on every Streamlit rerun.
    if (_WATCHDOG_THREAD is not None
            and _WATCHDOG_THREAD.is_alive()):
        return

    # Distinguish first-spawn vs respawn for log clarity
    is_respawn = _WATCHDOG_THREAD is not None

    _WATCHDOG_STOP_EVENT.clear()
    _WATCHDOG_THREAD = threading.Thread(
        target=_watchdog_loop, args=(my_pid,),
        name="bursa-watchdog", daemon=True,
    )
    _WATCHDOG_THREAD.start()
    if is_respawn:
        try:
            log_scheduler_event(
                "WATCHDOG_RESPAWN",
                f"PID {my_pid}: watchdog had died — respawning",
                "WARN",
            )
        except Exception:
            pass
    # First-spawn case logs WATCHDOG_STARTED from inside _watchdog_loop.


# Backwards-compat alias so any external/older call site keeps working.
_start_watchdog = _ensure_watchdog_running


def _stop_watchdog() -> None:
    """Signal + join the watchdog thread. Called from stop().

    Counterpart to ``_ensure_watchdog_running``.
    """
    global _WATCHDOG_THREAD
    _WATCHDOG_STOP_EVENT.set()
    if _WATCHDOG_THREAD is not None:
        try:
            _WATCHDOG_THREAD.join(timeout=3)
        except Exception:
            pass
    _WATCHDOG_THREAD = None


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
        cycle_started_at=None,   # v3.1.10: no cycle in flight yet
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
            # v3.1.10: also treat the watchdog sentinel as "owner changed"
            # so a stuck loop self-exits even before a new one starts.
            if (current_owner == WATCHDOG_TIMEOUT_OWNER_SENTINEL
                    or other_is_alive):
                # Live owner (or watchdog sentinel) — exit SILENTLY.
                if not getattr(_loop, "_silent_exit_logged", False):
                    log_scheduler_event(
                        "GHOST_EXIT",
                        f"PID {my_pid} exiting — owner PID {current_owner} "
                        f"(beat {age_sec:.0f}s ago)" if age_sec is not None
                        else f"PID {my_pid} exiting — owner_pid changed "
                             f"to {current_owner}",
                        "WARN")
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
                # v3.1.10: stamp cycle_started_at so the watchdog can see
                # if we get stuck. Cleared in the finally block.
                update_scheduler_state(cycle_started_at=myt_iso())
                try:
                    # v3.1.9: pass my_pid so _run_one_cycle can abort if
                    # ownership changed while it was sleeping.
                    summary = _run_one_cycle(autotrade=autotrade,
                                              autoexit=autoexit,
                                              my_pid=my_pid)
                    duration = time.time() - t0
                    update_scheduler_state(
                        last_run_at=myt_iso(),
                        next_run_at=myt_iso(_next_run_at(interval_sec)),
                        consecutive_failures=0,
                        last_error="",
                        cycle_started_at=None,
                    )
                    # v3.1.10: soft-warn on slow cycles even if they
                    # complete. Gives you visibility before the watchdog
                    # has to act.
                    if duration > CYCLE_DURATION_WARN_SEC:
                        log_scheduler_event(
                            "CYCLE_SLOW",
                            f"Cycle completed in {duration:.0f}s "
                            f"(> {CYCLE_DURATION_WARN_SEC}s warn threshold). "
                            "Yahoo Finance may be degraded — monitor for "
                            "CYCLE_TIMEOUT events.",
                            "WARN",
                            duration_sec=duration,
                        )
                    log_scheduler_event(
                        "CYCLE_OK",
                        f"scan={summary['scan_count']} settled={summary['settled']} "
                        f"partials={summary['partials']} entries={summary['auto_entries']}",
                        duration_sec=duration, payload=summary,
                    )
                finally:
                    # Defensive: clear cycle_started_at even on uncaught
                    # exceptions so the watchdog doesn't latch on the
                    # stamp from a cycle that already errored out.
                    try:
                        update_scheduler_state(cycle_started_at=None)
                    except Exception:
                        pass
            except Exception as e:
                tb = traceback.format_exc()
                fails = (state.get("consecutive_failures") or 0) + 1
                update_scheduler_state(consecutive_failures=fails,
                                       last_error=f"{e}\n{tb}",
                                       cycle_started_at=None)
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
                           last_heartbeat=myt_iso(),
                           cycle_started_at=None)
    log_scheduler_event("STOPPED", "Robo-Trader loop exited")


# -------------------------------------------------------------------------
# Public control
# -------------------------------------------------------------------------

def _find_live_non_orphan_scheduler_thread() -> threading.Thread | None:
    """
    Return the first alive thread named ``bursa-scheduler`` whose ident
    is NOT in ``_ORPHANED_THREAD_IDS``.

    v3.1.10: zombies that stop() asked to exit but couldn't kill within
    the join() window MUST be skipped here — otherwise start() Guard 2
    treats them as "live workers", adopts the handle, and returns False,
    permanently jamming the UI on STOPPED.
    """
    for t in threading.enumerate():
        if getattr(t, "name", None) != "bursa-scheduler":
            continue
        if not t.is_alive():
            continue
        if t.ident in _ORPHANED_THREAD_IDS:
            continue
        return t
    return None


def start(interval_sec: int = 3600) -> bool:
    """
    Start the background thread if not already running.

    Sets owner_pid to our process ID. Any older ghost loop (from a
    previous Streamlit Cloud deploy, OR a zombie from a stuck loop in
    this process) will detect the PID change on its next iteration and
    exit cleanly.

    v3.1.9: Guards 2 and 3 now correctly handle the case where the
    local _THREAD handle is lost/crashed but the DB still shows a
    fresh heartbeat. Previously this permanently blocked start().

    v3.1.10: Guard 2 now skips threads in ``_ORPHANED_THREAD_IDS`` —
    zombies that stop() requested to exit but that are still alive
    (typically blocked in a sleep or a network call). Without this
    skip, a single stuck cycle would permanently jam the UI on
    "🔴 STOPPED" with no way back to RUNNING. Also starts the
    runaway-cycle watchdog so the system can self-recover even
    without UI input.
    """
    global _THREAD
    with _LOCK:
        my_pid = os.getpid()

        # GC the orphan registry first — any zombie that has finally
        # exited gets cleared out, so future starts don't carry baggage.
        _gc_orphaned_thread_ids()

        # Guard 1: module-level handle (fast, local). Skip if it's an
        # orphan we already asked to die — it shouldn't normally be
        # reassigned here but defensive check is cheap.
        if (_THREAD is not None
                and _THREAD.is_alive()
                and _THREAD.ident not in _ORPHANED_THREAD_IDS):
            log_scheduler_event(
                "START_REJECT",
                f"PID {my_pid}: _THREAD handle points to alive thread",
                "INFO",
            )
            return False

        state = get_scheduler_state()
        db_running = bool(state.get("running", 0))
        db_owner = state.get("owner_pid", 0) or 0

        # Guard 2: scan threading.enumerate() for any alive
        # "bursa-scheduler" that is NOT a known orphan.
        found_alive = _find_live_non_orphan_scheduler_thread()

        if found_alive is not None:
            # Found a non-orphan alive scheduler thread. If our local
            # handle is dead/None, adopt it rather than spawning a
            # duplicate — this preserves the "single loop per process"
            # invariant.
            if _THREAD is None or not _THREAD.is_alive():
                _THREAD = found_alive
                log_scheduler_event(
                    "ADOPT_THREAD",
                    f"PID {my_pid}: adopted alive scheduler thread "
                    f"{found_alive.ident} (handle was lost)",
                    "WARN",
                )
                # v3.1.11: the watchdog must be alive WHENEVER the
                # scheduler is alive, regardless of how we got here.
                # The ADOPT_THREAD path used to skip this and was the
                # primary reason the user saw zero WATCHDOG_STARTED
                # events on Streamlit Cloud.
                _ensure_watchdog_running(my_pid)
                return False

            # _THREAD points to a different dead thread, but enumerate
            # found another alive non-orphan. Don't double-start; the
            # old loop will self-evict via owner_pid.
            if db_running and db_owner == my_pid:
                log_scheduler_event(
                    "START_REJECT",
                    f"PID {my_pid}: alive thread found, db_running=1 "
                    f"owner={db_owner} — not starting duplicate",
                    "INFO",
                )
                return False
            log_scheduler_event(
                "ZOMBIE_SKIP",
                f"PID {my_pid}: alive thread found but DB says "
                f"running={int(db_running)} owner={db_owner} — "
                f"starting new thread anyway",
                "WARN",
            )

        # Guard 3: DB heartbeat freshness for this PID.
        # ONLY block if we have a local alive (and non-orphan) thread.
        # If the thread crashed or has been orphaned by stop(), the DB
        # is stale — don't let it block recovery.
        thread_alive = (_THREAD is not None
                         and _THREAD.is_alive()
                         and _THREAD.ident not in _ORPHANED_THREAD_IDS)
        if thread_alive and (state.get("running") == 1 and state.get("owner_pid") == my_pid):
            hb = state.get("last_heartbeat")
            if hb:
                try:
                    hb_dt = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone(timedelta(hours=8)))
                    age = (get_myt_now() - hb_dt).total_seconds()
                    if age < 300:
                        log_scheduler_event(
                            "START_REJECT",
                            f"PID {my_pid}: heartbeat fresh ({age:.0f}s) — "
                            f"already running",
                            "INFO",
                        )
                        return False
                except Exception:
                    pass

        # v3.1.9: ONLY clear the stop event AFTER all guards pass.
        # Previously _STOP_EVENT.clear() was before Guard 2, which meant
        # a slow-dying old thread would see the event cleared and sleep
        # for up to an hour instead of exiting — permanently blocking start().
        _STOP_EVENT.clear()

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
            cycle_started_at=None,
        )
        _THREAD = threading.Thread(
            target=_loop, args=(interval_sec, my_pid),
            name="bursa-scheduler", daemon=True,
        )
        _THREAD.start()
        # v3.1.10: start (or restart) the runaway-cycle watchdog now
        # that we have a fresh owner_pid.
        _ensure_watchdog_running(my_pid)
        log_scheduler_event(
            "START_OK",
            f"PID {my_pid}: scheduler thread spawned",
            "INFO",
        )
        return True


def stop() -> None:
    """
    Request the thread to exit.

    v3.1.10: register the dying thread's ident in ``_ORPHANED_THREAD_IDS``
    BEFORE the bounded join() — so even if the thread is stuck inside a
    long network call and outlives the 5-second join, subsequent start()
    calls know to ignore it. This guarantees the UI can always recover
    from STOPPED → RUNNING. Also stops the watchdog so it doesn't
    hold a stale my_pid.
    """
    global _THREAD
    with _LOCK:
        _STOP_EVENT.set()
        # v3.1.9: clear owner_pid so start() knows this is a true stop.
        # Also set running=0 so Guard 2 in start() won't block on zombies.
        # v3.1.10: also clear cycle_started_at so the watchdog doesn't
        # latch onto a stamp from a cycle that was in flight.
        update_scheduler_state(kill_switch=1, running=0, owner_pid=0,
                               cycle_started_at=None)
        if _THREAD is not None:
            # v3.1.10: mark as orphaned BEFORE join — if join times out
            # because the thread is stuck, the orphan flag still applies.
            try:
                if _THREAD.ident is not None:
                    _ORPHANED_THREAD_IDS.add(_THREAD.ident)
            except Exception:
                pass
            _THREAD.join(timeout=5)
        _THREAD = None
        _stop_watchdog()


def is_running() -> bool:
    """
    True iff we have a local non-orphaned alive thread AND the DB still
    shows running=1. v3.1.10: explicitly excludes orphan threads so a
    zombie that survived stop() doesn't make the badge lie.
    """
    with _LOCK:
        alive = (_THREAD is not None
                 and _THREAD.is_alive()
                 and _THREAD.ident not in _ORPHANED_THREAD_IDS)
    state = get_scheduler_state()
    return alive and bool(state.get("running", 0))


def force_restart(interval_sec: int = 3600) -> None:
    """
    User-facing 'Force Reboot Robo-Trader'.

    v3.1.10: do NOT block waiting for the old thread to die. The old
    thread is now registered as an orphan (by stop()), so start()'s
    Guard 2 will correctly skip it. The orphan self-terminates on its
    next wake-up via the owner_pid mismatch check in _loop.

    Previously this function polled for up to 30 s waiting for the
    zombie to actually exit, which could still time out for threads
    stuck in a long network call — leaving the user with a permanently
    STOPPED scheduler. The orphan registry fixes that root cause.
    """
    stop()
    start(interval_sec=interval_sec)


def ensure_started(interval_sec: int = 3600,
                   max_heartbeat_age_sec: int | None = None) -> None:
    """
    Idempotent + self-healing.

    v3.1.8: be conservative — don't spawn duplicate loops.
    If another process is the owner and beat recently, do NOTHING.

    1. If another live owner exists → do nothing.
    2. If we own it and our local thread is alive → all good.
    3. If DB says we own it with fresh heartbeat but local thread handle
       is lost/crashed → force-restart.
    4. If stale/no owner → start.
    5. If our local thread is a ghost (DB shows other owner) → force-restart.

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
        # v3.1.11: but make sure the watchdog is also alive! Streamlit
        # reruns `app.py` on every interaction → this is the most-
        # travelled path in production. Before this fix, if the watchdog
        # died for any reason, nothing respawned it.
        _ensure_watchdog_running(my_pid)
        return

    # Case 3: DB says we own it with fresh heartbeat, but local thread handle
    # is lost (crashed, module reload, stop/join race). start() will reject
    # (Guard 3). Force-restart to recover.
    if current_owner == my_pid and not is_running():
        if bool(state.get("running", 0)) and age is not None and age < 300:
            log_scheduler_event(
                "SELF_HEAL",
                f"DB shows running with fresh heartbeat but local thread handle "
                f"lost ({age:.0f}s old) — force-restarting",
                "WARN",
            )
            force_restart(interval_sec=interval_sec)
            return

    # Case 4: genuinely stopped / stale owner → normal start
    if not is_running():
        start(interval_sec=interval_sec)
        return

    # Case 5: thread alive locally but DB shows another owner — defer to DB.
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


def get_watchdog_status() -> dict:
    """
    Read-only health summary for the UI to render in the 🤖 Robo-Trader
    tab. Cheap — one SQLite read + a few in-memory comparisons. Safe to
    call on every Streamlit rerun.

    The UI MUST go through this function rather than poking at module
    privates (``_WATCHDOG_THREAD``, ``_ORPHANED_THREAD_IDS`` etc.) so
    we can refactor internals without breaking the UI.

    Returns a dict with stable keys (see ``test_watchdog_status_ui.py``).
    Never raises — degrades gracefully to "best-effort" values if any
    subsystem is unreachable. The UI must always be renderable.
    """
    # Defaults — guarantee shape even on total failure
    status = {
        "watchdog_alive": False,
        "watchdog_thread_ident": None,
        "cycle_in_flight": False,
        "cycle_running_for_sec": None,
        "cycle_started_at": None,
        "recent_timeouts_24h": 0,
        "recent_slow_cycles_24h": 0,
        "recent_respawns_24h": 0,
        "orphan_thread_count": 0,
        "watchdog_timeout_sec": WATCHDOG_CYCLE_TIMEOUT_SEC,
        "cycle_warn_sec": CYCLE_DURATION_WARN_SEC,
    }

    # 1. Watchdog thread liveness — module globals, no DB needed
    try:
        wt = _WATCHDOG_THREAD
        if wt is not None and wt.is_alive():
            status["watchdog_alive"] = True
            status["watchdog_thread_ident"] = wt.ident
    except Exception:
        pass

    # 2. Orphan registry size
    try:
        status["orphan_thread_count"] = len(_ORPHANED_THREAD_IDS)
    except Exception:
        pass

    # 3. Cycle-in-flight + duration — from scheduler_state
    try:
        ss = get_scheduler_state()
        stamp = ss.get("cycle_started_at")
        if stamp:
            status["cycle_started_at"] = stamp
            status["cycle_in_flight"] = True
            try:
                started_dt = datetime.strptime(
                    stamp, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone(timedelta(hours=8)))
                age = (get_myt_now() - started_dt).total_seconds()
                status["cycle_running_for_sec"] = max(0.0, age)
            except Exception:
                # Bad timestamp format — leave cycle_running_for_sec None
                pass
    except Exception:
        pass

    # 4. Recent operational events from scheduler_log (last 24h)
    #    Cheap one-query summary so the user sees "anything bad lately?"
    try:
        from logger import get_scheduler_log
        recent = get_scheduler_log(limit=500) or []
        cutoff = get_myt_now() - timedelta(hours=24)
        for row in recent:
            ts = row.get("timestamp") or ""
            event = row.get("event") or ""
            # Cheap-ish timestamp parse — accept the standard format
            try:
                rt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone(timedelta(hours=8)))
                if rt < cutoff:
                    continue
            except Exception:
                # If we can't parse, count it anyway — safer to over-report
                pass
            if event == "CYCLE_TIMEOUT":
                status["recent_timeouts_24h"] += 1
            elif event == "CYCLE_SLOW":
                status["recent_slow_cycles_24h"] += 1
            elif event == "WATCHDOG_RESPAWN":
                status["recent_respawns_24h"] += 1
    except Exception:
        # Logger unreachable — counts stay 0, UI still renderable
        pass

    return status


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
