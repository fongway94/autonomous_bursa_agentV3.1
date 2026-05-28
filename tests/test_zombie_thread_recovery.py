"""
v3.1.10 regression: when a scheduler loop gets stuck (e.g. mid-yfinance
network hang during a cycle, or in a long _STOP_EVENT.wait() that
ignores kill_switch until next wake), pressing Stop / Kill-Switch /
Start / Force Restart from the UI must NOT permanently leave the agent
in STOPPED state.

The original bug (reported with screenshot):
  - last_run_at = 12:00:08 (stuck for >5 hours)
  - heartbeat   = 17:00:00 (loop woke briefly but couldn't make progress)
  - badge: 🔴 STOPPED
  - clicking Start → nothing (Guard 2 adopts the zombie, returns False)
  - clicking Force Restart → also fails because the zombie won't die
    within stop()'s 5-second join timeout and force_restart's 30-second
    poll, so start() rejects again.

The fix has TWO parts:

  1. ``stop()`` must mark the zombie thread as orphaned (via a per-thread
     flag the loop checks every iteration), and ``force_restart()`` must
     allow a NEW thread to start even if the old one is still alive — the
     old loop self-terminates on its next wake because the
     ``owner_pid`` no longer matches and it sees the orphaned-flag.

  2. ``start()`` Guard 2 must NOT adopt a thread that has been marked
     orphaned by a previous stop() — that thread is "dying", not "alive".
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta


def _reset_scheduler_module_state(scheduler):
    """Cleanly tear down anything left from a previous test."""
    scheduler._STOP_EVENT.set()
    if scheduler._THREAD is not None and scheduler._THREAD.is_alive():
        scheduler._THREAD.join(timeout=2)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    # Clear any module-level flags set by previous tests
    if hasattr(scheduler._loop, "_silent_exit_logged"):
        delattr(scheduler._loop, "_silent_exit_logged")


def test_force_restart_recovers_from_unresponsive_loop(monkeypatch):
    """
    Reproduces the live bug: a long-running thread that's stuck inside
    a sleep or a network call won't honor stop()'s 5-second join.
    force_restart() MUST be able to spin up a new working thread
    regardless — that's the entire point of a "force" action.
    """
    import scheduler
    from repository import update_scheduler_state, get_scheduler_state
    from db import myt_iso, get_myt_now

    _reset_scheduler_module_state(scheduler)

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                         "partials": 0, "auto_entries": 0,
                                         "rejected": 0, "errors": []})

    # Simulate a stuck zombie thread: alive, named "bursa-scheduler",
    # but in a long sleep that won't honor _STOP_EVENT quickly.
    zombie_stuck = threading.Event()
    zombie_done = threading.Event()

    def _zombie_target():
        # Pretend we're stuck inside a slow network call — we IGNORE
        # _STOP_EVENT for a long time. This mimics yfinance hangs.
        zombie_stuck.wait(timeout=10)  # released by test cleanup at the end
        zombie_done.set()

    zombie = threading.Thread(target=_zombie_target,
                              name="bursa-scheduler", daemon=True)
    zombie.start()
    scheduler._THREAD = zombie

    # DB state mirrors what the live bug screenshot shows:
    # stale heartbeat, owner_pid was old PID, kill_switch may have been
    # toggled by the user clicking "Kill-Switch".
    stale_pid = 12345
    stale_hb = myt_iso(get_myt_now() - timedelta(minutes=32))
    update_scheduler_state(
        owner_pid=stale_pid,
        last_heartbeat=stale_hb,
        running=1,
        kill_switch=0,
    )

    try:
        # User clicks "♻️ Force Restart" → must succeed
        scheduler.force_restart(interval_sec=60)
        time.sleep(0.4)  # let new thread spin up

        # ASSERTIONS:
        # 1. is_running() must report True
        assert scheduler.is_running(), (
            "force_restart MUST leave is_running() == True even if "
            "a previous zombie thread is still alive"
        )

        # 2. owner_pid in DB must be the CURRENT PID
        new_state = get_scheduler_state()
        assert new_state.get("owner_pid", 0) == os.getpid(), (
            f"owner_pid should be current PID, got {new_state.get('owner_pid')}"
        )

        # 3. scheduler_state.running must be 1
        assert new_state.get("running") == 1, (
            "scheduler_state.running must be 1 after force_restart"
        )

        # 4. The local _THREAD handle must point to a NEW alive thread,
        #    not the zombie
        assert scheduler._THREAD is not None
        assert scheduler._THREAD.is_alive()
        assert scheduler._THREAD is not zombie, (
            "force_restart must NOT adopt the stuck zombie thread"
        )

    finally:
        # Tell zombie it can exit
        zombie_stuck.set()
        scheduler.stop()
        # Drain
        for _ in range(50):
            if not zombie.is_alive():
                break
            time.sleep(0.1)


def test_start_after_kill_switch_recovers_even_if_zombie_alive(monkeypatch):
    """
    Reproduces the live UX flow:
      1. User clicks 🚨 Kill-Switch → stop() runs; zombie can't die in time
      2. User clears kill-switch in Settings
      3. User clicks ▶️ Start
      4. Start MUST succeed — currently fails because Guard 2 adopts
         the still-alive zombie and returns False.
    """
    import scheduler
    from repository import update_scheduler_state, get_scheduler_state
    from db import myt_iso, get_myt_now

    _reset_scheduler_module_state(scheduler)

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                         "partials": 0, "auto_entries": 0,
                                         "rejected": 0, "errors": []})

    # Simulate stuck zombie
    zombie_release = threading.Event()

    def _zombie_target():
        zombie_release.wait(timeout=10)

    zombie = threading.Thread(target=_zombie_target,
                              name="bursa-scheduler", daemon=True)
    zombie.start()

    # User clicks Kill-Switch — stop() sets kill_switch=1, running=0,
    # owner_pid=0, joins for 5s, gives up, sets _THREAD=None
    scheduler._THREAD = zombie
    scheduler.stop()
    assert zombie.is_alive(), (
        "test precondition: zombie must still be alive after stop() "
        "since it ignores _STOP_EVENT"
    )

    # User clears kill-switch (Settings → ✅ Clear kill-switch)
    update_scheduler_state(kill_switch=0)

    # User clicks ▶️ Start — this is the click that's currently silently
    # failing in production.
    try:
        ok = scheduler.start(interval_sec=60)
        time.sleep(0.4)

        assert ok, (
            "start() MUST return True after kill-switch flow even if "
            "a stuck zombie thread is still alive. The zombie will "
            "self-terminate on next wake via owner_pid mismatch."
        )

        assert scheduler.is_running(), (
            "is_running() must be True immediately after a successful start()"
        )

        new_state = get_scheduler_state()
        assert new_state.get("owner_pid", 0) == os.getpid()
        assert new_state.get("running") == 1

        assert scheduler._THREAD is not None
        assert scheduler._THREAD.is_alive()
        assert scheduler._THREAD is not zombie

    finally:
        zombie_release.set()
        scheduler.stop()
        for _ in range(50):
            if not zombie.is_alive():
                break
            time.sleep(0.1)


def test_orphaned_zombie_self_terminates_on_next_wake(monkeypatch):
    """
    The flip side: once a new thread takes over, the old zombie MUST
    exit cleanly on its next loop iteration via the owner_pid check.
    Otherwise we'd leak threads forever.
    """
    import scheduler
    from repository import update_scheduler_state, get_scheduler_state
    from db import myt_iso, get_myt_now
    from logger import get_scheduler_log

    _reset_scheduler_module_state(scheduler)

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                         "partials": 0, "auto_entries": 0,
                                         "rejected": 0, "errors": []})

    # Set DB to show some "current" owner (the new PID we're about to claim)
    new_pid = os.getpid()
    update_scheduler_state(
        owner_pid=new_pid,
        last_heartbeat=myt_iso(get_myt_now()),
        running=1,
        kill_switch=0,
    )

    # Spawn a "zombie" loop with an OLD pid using the real _loop function.
    # It should detect on first iteration that owner_pid != my_pid and exit.
    old_pid = 99999
    # Pre-reset the silent-exit flag
    if hasattr(scheduler._loop, "_silent_exit_logged"):
        delattr(scheduler._loop, "_silent_exit_logged")
    scheduler._STOP_EVENT.clear()
    zombie = threading.Thread(
        target=scheduler._loop, args=(60, old_pid),
        name="zombie-test", daemon=True)
    zombie.start()

    # Give it a moment to wake, check ownership, exit
    time.sleep(0.5)
    scheduler._STOP_EVENT.set()
    zombie.join(timeout=3)

    assert not zombie.is_alive(), (
        "Zombie loop must self-exit when owner_pid in DB does not match its PID"
    )

    # Cleanup
    scheduler._STOP_EVENT.clear()
