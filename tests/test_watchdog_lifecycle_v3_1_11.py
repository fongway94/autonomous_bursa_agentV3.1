"""
v3.1.11 regression: the watchdog wasn't spawning in several real-world
Streamlit Cloud code paths because `_start_watchdog()` was only called
from ONE place — the end of a successful `start()`. These paths all
skipped it:

  1. `start()` short-circuits via ADOPT_THREAD when a previously-spawned
     scheduler thread is still alive but our local _THREAD handle was
     lost. The user reported seeing ADOPT_THREAD events in their live
     scheduler log but ZERO WATCHDOG_STARTED events — proof this path
     was being hit in production.

  2. `start()` returns False with START_REJECT when the local _THREAD is
     alive (most common normal-running case after the first successful
     start).

  3. `ensure_started()` Case 2 returns early when the scheduler is
     healthy and we own it — never calls start(), so the watchdog never
     spawns. This is the path Streamlit Cloud takes on EVERY rerun
     after the initial boot, so if the watchdog wasn't spawned on the
     very first start, it never spawns at all.

  4. If the watchdog thread itself died (uncaught exception, memory
     pressure, etc.), nothing respawned it because `_start_watchdog`'s
     "skip if already alive" check is correct in isolation but had no
     liveness-driven respawn caller.

Fix: introduce `_ensure_watchdog_running(my_pid)` — idempotent, safe to
call from every path that "knows the scheduler is in good shape".
Wired into:
  - `start()` end (already there)
  - `start()` ADOPT_THREAD path (new)
  - `ensure_started()` Case 2 (new — most important for Streamlit Cloud)

Together these guarantee the watchdog is alive whenever the scheduler
loop is alive, regardless of how the process got there.
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta


def _reset_scheduler_state(scheduler):
    """Tear down anything left from a previous test."""
    scheduler._STOP_EVENT.set()
    if scheduler._THREAD is not None and scheduler._THREAD.is_alive():
        scheduler._THREAD.join(timeout=2)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    scheduler._WATCHDOG_STOP_EVENT.set()
    if scheduler._WATCHDOG_THREAD is not None and scheduler._WATCHDOG_THREAD.is_alive():
        scheduler._WATCHDOG_THREAD.join(timeout=2)
    scheduler._WATCHDOG_THREAD = None
    scheduler._WATCHDOG_STOP_EVENT.clear()
    scheduler._ORPHANED_THREAD_IDS.clear()
    if hasattr(scheduler._loop, "_silent_exit_logged"):
        delattr(scheduler._loop, "_silent_exit_logged")


def test_ensure_started_spawns_watchdog_when_scheduler_already_running(monkeypatch):
    """
    THE production bug: user sees ADOPT_THREAD events but no
    WATCHDOG_STARTED events. Every Streamlit rerun calls
    ensure_started() → Case 2 (we own a healthy scheduler) → return
    without ever spawning the watchdog.

    After the fix, ensure_started() must spawn (or confirm alive) the
    watchdog on every call, idempotently.
    """
    import scheduler
    from repository import update_scheduler_state
    from db import myt_iso, get_myt_now

    _reset_scheduler_state(scheduler)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})

    # Simulate "scheduler is already running, we own it, heartbeat is
    # fresh" — exactly the state that triggers ensure_started() Case 2.
    my_pid = os.getpid()
    # Spawn a real scheduler thread so is_running() returns True
    ok = scheduler.start(interval_sec=60)
    assert ok, "test precondition: scheduler must start cleanly"
    # Kill the watchdog only — simulate it having died on its own
    scheduler._WATCHDOG_STOP_EVENT.set()
    if scheduler._WATCHDOG_THREAD is not None:
        scheduler._WATCHDOG_THREAD.join(timeout=2)
    scheduler._WATCHDOG_THREAD = None
    scheduler._WATCHDOG_STOP_EVENT.clear()
    assert scheduler._WATCHDOG_THREAD is None, "test precondition: watchdog killed"
    # But the scheduler loop must still be alive
    assert scheduler.is_running(), "scheduler must still be running"

    try:
        # NOW call ensure_started — this is what Streamlit does every rerun
        scheduler.ensure_started(interval_sec=60)
        time.sleep(0.2)

        # ASSERTION: the watchdog must have been respawned
        assert scheduler._WATCHDOG_THREAD is not None, (
            "ensure_started() MUST spawn the watchdog even when the "
            "scheduler loop is already running. Otherwise Streamlit Cloud "
            "users who hit the ADOPT_THREAD path or any healthy-on-boot "
            "path get NO watchdog ever — and the v3.1.10 autonomous "
            "recovery silently doesn't work."
        )
        assert scheduler._WATCHDOG_THREAD.is_alive(), (
            "respawned watchdog must be alive"
        )
    finally:
        scheduler.stop()


def test_start_adopt_thread_path_spawns_watchdog(monkeypatch):
    """
    The user's screenshot showed multiple ADOPT_THREAD events in the
    scheduler log. ADOPT_THREAD fires when start() finds a live
    scheduler thread but the local _THREAD handle was lost (typical
    after a Streamlit script reload).

    Before the fix, this path `return False`d without spawning the
    watchdog. After the fix, the watchdog must spawn here too.
    """
    import scheduler
    from repository import update_scheduler_state
    from db import myt_iso, get_myt_now

    _reset_scheduler_state(scheduler)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})

    # First start — normal path
    ok = scheduler.start(interval_sec=60)
    assert ok
    surviving_thread = scheduler._THREAD
    surviving_thread_ident = surviving_thread.ident
    assert surviving_thread is not None and surviving_thread.is_alive()

    # Kill the watchdog AND drop the local _THREAD handle, simulating
    # what Streamlit can do on a script reload: the daemon thread keeps
    # running in the process but the module-level variable gets reset.
    scheduler._WATCHDOG_STOP_EVENT.set()
    if scheduler._WATCHDOG_THREAD is not None:
        scheduler._WATCHDOG_THREAD.join(timeout=2)
    scheduler._WATCHDOG_THREAD = None
    scheduler._WATCHDOG_STOP_EVENT.clear()
    scheduler._THREAD = None  # local handle lost, real thread still alive
    assert surviving_thread.is_alive(), "test precondition: thread still alive"

    try:
        # User clicks Start (or app.py just calls start() on boot).
        # This must hit the ADOPT_THREAD branch.
        result = scheduler.start(interval_sec=60)
        # start() returns False because it adopted rather than spawned
        assert result is False, "expected ADOPT_THREAD branch (False)"
        # But the watchdog MUST be alive
        time.sleep(0.2)
        assert scheduler._WATCHDOG_THREAD is not None, (
            "start()'s ADOPT_THREAD branch must spawn the watchdog "
            "before returning False — otherwise the live production path "
            "has no autonomous recovery"
        )
        assert scheduler._WATCHDOG_THREAD.is_alive()
    finally:
        scheduler.stop()


def test_ensure_watchdog_running_is_idempotent(monkeypatch):
    """
    Calling the new helper twice in quick succession must NOT spawn two
    watchdog threads — otherwise we'd leak threads on every Streamlit
    rerun.
    """
    import scheduler

    _reset_scheduler_state(scheduler)

    my_pid = os.getpid()
    try:
        # First call — should spawn
        scheduler._ensure_watchdog_running(my_pid)
        time.sleep(0.1)
        first = scheduler._WATCHDOG_THREAD
        assert first is not None and first.is_alive(), "first call must spawn"

        # Second call — must NOT spawn a new one
        scheduler._ensure_watchdog_running(my_pid)
        time.sleep(0.1)
        second = scheduler._WATCHDOG_THREAD
        assert second is first, (
            "idempotency: second call must NOT replace the live watchdog"
        )

        # Third call after explicitly killing it — must respawn
        scheduler._WATCHDOG_STOP_EVENT.set()
        first.join(timeout=2)
        scheduler._WATCHDOG_THREAD = None
        scheduler._WATCHDOG_STOP_EVENT.clear()
        scheduler._ensure_watchdog_running(my_pid)
        time.sleep(0.1)
        third = scheduler._WATCHDOG_THREAD
        assert third is not None and third.is_alive(), (
            "third call after kill must spawn a fresh watchdog"
        )
        assert third is not first, (
            "respawned watchdog must be a NEW thread object"
        )
    finally:
        scheduler._WATCHDOG_STOP_EVENT.set()
        if scheduler._WATCHDOG_THREAD is not None:
            scheduler._WATCHDOG_THREAD.join(timeout=2)
        scheduler._WATCHDOG_THREAD = None
        scheduler._WATCHDOG_STOP_EVENT.clear()


def test_watchdog_logs_respawn_event_when_recreated(monkeypatch):
    """
    For ops visibility: when the watchdog gets respawned by
    ensure_started (because it had died), we want a WATCHDOG_RESPAWN
    log row so the user can see it in 📜 Logs. Otherwise debugging
    "the watchdog keeps dying" would be invisible.
    """
    import scheduler
    from logger import get_scheduler_log

    _reset_scheduler_state(scheduler)
    my_pid = os.getpid()

    try:
        # Spawn watchdog initially (logs WATCHDOG_STARTED)
        scheduler._ensure_watchdog_running(my_pid)
        time.sleep(0.1)

        # Kill it
        scheduler._WATCHDOG_STOP_EVENT.set()
        if scheduler._WATCHDOG_THREAD is not None:
            scheduler._WATCHDOG_THREAD.join(timeout=2)
        scheduler._WATCHDOG_THREAD = None
        scheduler._WATCHDOG_STOP_EVENT.clear()

        # Respawn — should log a distinguishable event
        scheduler._ensure_watchdog_running(my_pid)
        time.sleep(0.2)

        recent = get_scheduler_log(limit=20)
        respawn_events = [r for r in recent
                          if r["event"] in ("WATCHDOG_RESPAWN",
                                             "WATCHDOG_STARTED")]
        assert len(respawn_events) >= 2, (
            f"expected at least 2 watchdog start events (initial + respawn), "
            f"got {len(respawn_events)}: "
            f"{[r['event'] for r in respawn_events]}"
        )
    finally:
        scheduler._WATCHDOG_STOP_EVENT.set()
        if scheduler._WATCHDOG_THREAD is not None:
            scheduler._WATCHDOG_THREAD.join(timeout=2)
        scheduler._WATCHDOG_THREAD = None
        scheduler._WATCHDOG_STOP_EVENT.clear()
