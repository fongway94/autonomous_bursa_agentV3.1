"""
v3.2 scheduler lifecycle regression tests.

These tests cover the bugs that the v3.2 refactor fixed:

1. ADOPT_THREAD didn't write DB state → adopted thread self-killed
2. stop() set kill_switch=1 → force_restart left kill_switch=1 after adopt
3. ensure_started deferred to dead containers with fresh heartbeats
4. force_restart() left scheduler permanently STOPPED when old thread survived
"""

import os
import time
import threading
from datetime import timedelta


def test_force_restart_always_results_in_running(monkeypatch):
    """
    REGRESSION: force_restart() → stop() → start() must ALWAYS end with
    is_running() == True, regardless of whether old threads are still alive.

    Previously, stop() set kill_switch=1, then start() took the ADOPT_THREAD
    path (returning False without clearing kill_switch), leaving the
    scheduler permanently STOPPED.
    """
    import scheduler
    from repository import get_scheduler_state

    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Start scheduler
    assert scheduler.start(interval_sec=60)
    time.sleep(0.3)
    assert scheduler.is_running()

    # Force restart
    scheduler.force_restart(interval_sec=60)
    time.sleep(0.3)

    # MUST be running after force_restart
    assert scheduler.is_running(), \
        "force_restart must always end with scheduler running"

    state = get_scheduler_state()
    assert state["running"] == 1
    assert state["kill_switch"] == 0, \
        "force_restart must not leave kill_switch=1"
    assert state["owner_pid"] == os.getpid()

    scheduler.stop()


def test_stop_does_not_set_kill_switch():
    """
    REGRESSION: stop() must NOT set kill_switch=1. Only the dedicated
    Kill-Switch button (engage_kill_switch) should set it.

    Previously stop() set kill_switch=1, which meant force_restart()
    → stop() → start() left kill_switch=1 in the DB through the
    ADOPT_THREAD path.
    """
    import scheduler
    from repository import get_scheduler_state, update_scheduler_state

    # Start and stop
    scheduler.start(interval_sec=60)
    time.sleep(0.3)
    scheduler.stop()
    time.sleep(0.3)

    state = get_scheduler_state()
    assert state["kill_switch"] == 0, \
        "stop() must NOT set kill_switch — only engage_kill_switch() should"
    assert state["running"] == 0


def test_engage_kill_switch_does_set_it():
    """engage_kill_switch() must set kill_switch=1."""
    import scheduler
    from repository import get_scheduler_state

    scheduler.start(interval_sec=60)
    time.sleep(0.3)
    scheduler.engage_kill_switch()
    time.sleep(0.3)

    state = get_scheduler_state()
    assert state["kill_switch"] == 1
    assert state["running"] == 0


def test_start_after_engage_kill_switch_clears_it():
    """start() must clear kill_switch so the loop doesn't immediately exit."""
    import scheduler
    from repository import get_scheduler_state

    scheduler.start(interval_sec=60)
    time.sleep(0.3)
    scheduler.engage_kill_switch()
    time.sleep(0.3)

    # Now start again
    assert scheduler.start(interval_sec=60)
    time.sleep(0.3)

    state = get_scheduler_state()
    assert state["kill_switch"] == 0
    assert state["running"] == 1
    assert scheduler.is_running()

    scheduler.stop()


def test_ensure_started_starts_even_with_stale_owner(monkeypatch):
    """
    REGRESSION: ensure_started() must start the scheduler even when the
    DB shows another PID as owner with a recent heartbeat.

    On Streamlit Cloud, a Gist restore can bring back a DB with
    running=1, owner_pid=<dead container's PID>, and a heartbeat
    from seconds ago. The old ensure_started deferred to this ghost
    for up to 5 minutes, leaving the user with a dead scheduler.
    """
    import scheduler
    from repository import update_scheduler_state, get_scheduler_state
    from db import myt_iso, get_myt_now

    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Simulate Gist-restored state from dead container
    update_scheduler_state(
        running=1,
        owner_pid=99999,  # dead container PID
        last_heartbeat=myt_iso(),  # fresh (just restored)
        kill_switch=0,
    )

    scheduler.ensure_started(interval_sec=60)
    time.sleep(0.3)

    assert scheduler.is_running(), \
        "ensure_started must start even when another owner has fresh heartbeat"
    state = get_scheduler_state()
    assert state["owner_pid"] == os.getpid()
    assert state["running"] == 1

    scheduler.stop()


def test_start_orphans_stale_threads_from_module_reload(monkeypatch):
    """
    REGRESSION: When start() finds alive bursa-scheduler threads that
    are NOT our _THREAD, it must orphan them and start fresh — not adopt.

    The ADOPT_THREAD path was the root cause of the permanently-STOPPED
    bug because it returned False without writing DB state.
    """
    import scheduler
    from repository import get_scheduler_state

    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Start normally
    scheduler.start(interval_sec=60)
    time.sleep(0.3)
    old_thread = scheduler._THREAD
    old_ident = old_thread.ident

    # Simulate module state loss (handle dropped but thread alive)
    scheduler._THREAD = None
    scheduler._ORPHANED_THREAD_IDS.clear()

    # start() must orphan the old thread and create a new one
    assert scheduler.start(interval_sec=60) is True
    time.sleep(0.3)

    assert scheduler._THREAD is not old_thread
    assert old_ident in scheduler._ORPHANED_THREAD_IDS
    assert scheduler.is_running()

    state = get_scheduler_state()
    assert state["running"] == 1
    assert state["kill_switch"] == 0

    scheduler.stop()


def test_force_restart_with_stuck_thread(monkeypatch):
    """
    REGRESSION: force_restart() when the old thread survives the 5-second
    join must still result in is_running() == True.

    This was the exact production bug: stop() joined for 5s, thread
    survived (stuck in sleep), stop() set _THREAD=None + orphaned it,
    then start() found the old thread via enumerate() → ADOPT_THREAD
    → returned False without fixing DB → permanently STOPPED.
    """
    import scheduler

    cycle_blocking = threading.Event()

    def blocking_cycle(*a, **k):
        cycle_blocking.wait(timeout=30)
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", blocking_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: True)

    # Start and wait for cycle to begin
    scheduler.start(interval_sec=1)
    time.sleep(0.5)

    # The thread is now stuck in blocking_cycle
    old_thread = scheduler._THREAD

    # Force restart — stop() will fail to join (thread is stuck)
    scheduler.force_restart(interval_sec=60)
    time.sleep(0.3)

    # MUST be running regardless
    assert scheduler.is_running(), \
        "force_restart must result in running=True even if old thread survived join"

    # Release the stuck cycle so the old thread can exit
    cycle_blocking.set()
    time.sleep(1)

    # Still running (the new thread, not the old one)
    assert scheduler.is_running()

    scheduler.stop()
    time.sleep(0.5)
