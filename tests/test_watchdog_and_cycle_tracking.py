"""
v3.1.10 regression tests for the runaway-cycle watchdog and
cycle_started_at tracking.

The watchdog is the *autonomous* recovery path — the orphan registry
(test_zombie_thread_recovery.py) is the *user-initiated* recovery path.
Together they ensure the scheduler can't get permanently stuck:

  * Orphan registry: user clicks Start → can always recover
  * Watchdog:       system recovers within WATCHDOG_CYCLE_TIMEOUT_SEC
                    even if no one is watching

Background: the live bug that prompted v3.1.10 showed the scheduler
stuck for >5 hours with no autonomous recovery. The orphan fix alone
would require the user to notice + click. The watchdog closes that gap.
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta


# -------------------------------------------------------------------------
# cycle_started_at tracking
# -------------------------------------------------------------------------

def test_cycle_started_at_column_exists_after_init():
    """
    Schema migration must add cycle_started_at to scheduler_state.
    """
    from db import connect
    with connect(readonly=True) as c:
        cols = [r["name"] for r in c.execute(
            "PRAGMA table_info(scheduler_state)").fetchall()]
    assert "cycle_started_at" in cols, (
        "cycle_started_at column must be added by init_db migration "
        "for the v3.1.10 watchdog to function"
    )


def test_cycle_started_at_set_and_cleared_around_run_one_cycle(monkeypatch):
    """
    _loop must stamp cycle_started_at before the cycle, then clear it
    when the cycle finishes (or errors). Otherwise the watchdog has no
    way to know whether a cycle is in flight.

    We patch _next_run_at to return "almost now" so the loop's startup
    debounce fires immediately instead of waiting for the next hour
    boundary on the test machine's clock.
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import get_myt_now

    # Make _is_market_hours return True so we enter the cycle branch
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: True)
    # Short-circuit the debounce: next_run_at = now + tiny delta
    monkeypatch.setattr(scheduler, "_next_run_at",
                        lambda interval_sec: get_myt_now() + timedelta(seconds=1))

    observed_stamps = []

    def fake_cycle(*a, **k):
        # When _run_one_cycle is invoked, cycle_started_at must be set
        st = get_scheduler_state()
        observed_stamps.append(st.get("cycle_started_at"))
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)

    my_pid = os.getpid()
    update_scheduler_state(owner_pid=my_pid, running=1, kill_switch=0,
                            cycle_started_at=None)
    scheduler._STOP_EVENT.clear()

    t = threading.Thread(target=scheduler._loop, args=(60, my_pid),
                          name="bursa-scheduler", daemon=True)
    t.start()
    # Poll for the fake cycle to fire
    deadline = time.time() + 8.0
    while time.time() < deadline and not observed_stamps:
        time.sleep(0.1)
    scheduler._STOP_EVENT.set()
    t.join(timeout=3)

    # ASSERTIONS:
    # When the cycle fired, cycle_started_at must have been non-NULL
    # (set by _loop right before invoking _run_one_cycle).
    assert observed_stamps, (
        "fake_cycle was never invoked — debounce may have outlasted "
        "the test window"
    )
    assert observed_stamps[0] is not None, (
        "cycle_started_at must be set BEFORE _run_one_cycle is called"
    )

    # After the cycle returns and the loop finishes its post-cycle
    # bookkeeping, cycle_started_at must be cleared.
    final = get_scheduler_state()
    assert final.get("cycle_started_at") is None, (
        f"cycle_started_at must be cleared after a successful cycle, "
        f"got {final.get('cycle_started_at')!r}"
    )


def test_cycle_started_at_cleared_on_cycle_exception(monkeypatch):
    """
    Even if _run_one_cycle raises, cycle_started_at must be cleared.
    Otherwise the watchdog would think a cycle is permanently in flight.
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import myt_iso, get_myt_now

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: True)
    monkeypatch.setattr(scheduler, "_next_run_at",
                        lambda interval_sec: get_myt_now() + timedelta(seconds=1))

    def crashing_cycle(*a, **k):
        raise RuntimeError("simulated cycle crash")

    monkeypatch.setattr(scheduler, "_run_one_cycle", crashing_cycle)

    my_pid = os.getpid()
    update_scheduler_state(owner_pid=my_pid, running=1, kill_switch=0,
                            cycle_started_at=None)
    scheduler._STOP_EVENT.clear()

    t = threading.Thread(target=scheduler._loop, args=(60, my_pid),
                          name="bursa-scheduler", daemon=True)
    t.start()
    # Poll for the crash to happen + cleanup to run
    deadline = time.time() + 6.0
    while time.time() < deadline:
        st = get_scheduler_state()
        # Once consecutive_failures > 0 we know the crash happened
        if (st.get("consecutive_failures") or 0) > 0:
            break
        time.sleep(0.1)
    scheduler._STOP_EVENT.set()
    t.join(timeout=3)

    final = get_scheduler_state()
    assert (final.get("consecutive_failures") or 0) > 0, (
        "test precondition: the crashing cycle should have been invoked"
    )
    assert final.get("cycle_started_at") is None, (
        "cycle_started_at must be NULL after a cycle exception — "
        "otherwise the watchdog will falsely flag the stamp as a "
        "runaway cycle on the next process restart"
    )


# -------------------------------------------------------------------------
# Watchdog behaviour
# -------------------------------------------------------------------------

def test_watchdog_detects_runaway_cycle_and_forces_handoff(monkeypatch):
    """
    Core regression: if cycle_started_at is older than
    WATCHDOG_CYCLE_TIMEOUT_SEC, the watchdog must:
      1. Log CYCLE_TIMEOUT
      2. Clear cycle_started_at
      3. Bump owner_pid to the sentinel (-1)
      4. Set last_error so the UI shows the user what happened
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import myt_iso, get_myt_now
    from logger import get_scheduler_log

    # Speed the watchdog up so the test doesn't take 10 minutes.
    monkeypatch.setattr(scheduler, "WATCHDOG_CYCLE_TIMEOUT_SEC", 2)
    monkeypatch.setattr(scheduler, "WATCHDOG_TICK_SEC", 1)

    my_pid = os.getpid()
    # Stamp a cycle as having started 5 seconds ago — already past the
    # 2-second timeout.
    old_stamp = myt_iso(get_myt_now() - timedelta(seconds=5))
    update_scheduler_state(owner_pid=my_pid, running=1, kill_switch=0,
                            cycle_started_at=old_stamp)

    # Run the watchdog standalone (don't spawn the full scheduler)
    scheduler._WATCHDOG_STOP_EVENT.clear()
    wd = threading.Thread(target=scheduler._watchdog_loop,
                            args=(my_pid,),
                            name="bursa-watchdog-test", daemon=True)
    wd.start()
    # Give the watchdog one full tick to fire
    time.sleep(2.0)
    scheduler._WATCHDOG_STOP_EVENT.set()
    wd.join(timeout=3)

    final = get_scheduler_state()

    # 1. cycle_started_at cleared
    assert final.get("cycle_started_at") is None, (
        "watchdog must clear cycle_started_at on timeout"
    )

    # 2. owner_pid bumped to sentinel
    assert final.get("owner_pid") == scheduler.WATCHDOG_TIMEOUT_OWNER_SENTINEL, (
        f"watchdog must bump owner_pid to "
        f"{scheduler.WATCHDOG_TIMEOUT_OWNER_SENTINEL}, "
        f"got {final.get('owner_pid')}"
    )

    # 3. running=0
    assert final.get("running") == 0

    # 4. last_error mentions the watchdog
    err = final.get("last_error") or ""
    assert "Watchdog" in err or "watchdog" in err.lower(), (
        f"last_error should explain the watchdog action, got: {err!r}"
    )

    # 5. CYCLE_TIMEOUT event logged
    log_rows = get_scheduler_log(limit=20)
    assert any(r["event"] == "CYCLE_TIMEOUT" for r in log_rows), (
        "watchdog must log a CYCLE_TIMEOUT event"
    )


def test_watchdog_does_not_fire_for_healthy_cycle(monkeypatch):
    """
    If cycle_started_at is recent (< timeout), watchdog must do nothing.
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import myt_iso, get_myt_now
    from logger import get_scheduler_log

    monkeypatch.setattr(scheduler, "WATCHDOG_CYCLE_TIMEOUT_SEC", 10)
    monkeypatch.setattr(scheduler, "WATCHDOG_TICK_SEC", 1)

    my_pid = os.getpid()
    # Stamp a cycle as having started 1 second ago — well under 10s timeout
    fresh_stamp = myt_iso(get_myt_now() - timedelta(seconds=1))
    update_scheduler_state(owner_pid=my_pid, running=1, kill_switch=0,
                            cycle_started_at=fresh_stamp)

    scheduler._WATCHDOG_STOP_EVENT.clear()
    wd = threading.Thread(target=scheduler._watchdog_loop,
                            args=(my_pid,),
                            name="bursa-watchdog-test", daemon=True)
    wd.start()
    time.sleep(1.5)
    scheduler._WATCHDOG_STOP_EVENT.set()
    wd.join(timeout=3)

    final = get_scheduler_state()
    # owner_pid must still be ours, not the sentinel
    assert final.get("owner_pid") == my_pid, (
        "healthy cycle: owner_pid must stay unchanged"
    )
    # cycle_started_at must NOT be cleared by the watchdog
    assert final.get("cycle_started_at") == fresh_stamp, (
        "watchdog must not clear cycle_started_at for healthy cycles"
    )

    log_rows = get_scheduler_log(limit=20)
    assert not any(r["event"] == "CYCLE_TIMEOUT" for r in log_rows), (
        "watchdog must not log CYCLE_TIMEOUT for healthy cycles"
    )


def test_watchdog_skips_when_owner_pid_belongs_to_other_process(monkeypatch):
    """
    The watchdog must NOT act if scheduler_state.owner_pid != its my_pid.
    Otherwise two processes running side-by-side would both try to evict
    each other's cycles.
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import myt_iso, get_myt_now
    from logger import get_scheduler_log

    monkeypatch.setattr(scheduler, "WATCHDOG_CYCLE_TIMEOUT_SEC", 2)
    monkeypatch.setattr(scheduler, "WATCHDOG_TICK_SEC", 1)

    # Owner is a different PID — set cycle_started_at to ages ago
    other_pid = 88888
    old_stamp = myt_iso(get_myt_now() - timedelta(minutes=30))
    update_scheduler_state(owner_pid=other_pid, running=1, kill_switch=0,
                            cycle_started_at=old_stamp)

    # Run our watchdog with our PID
    my_pid = os.getpid()
    scheduler._WATCHDOG_STOP_EVENT.clear()
    wd = threading.Thread(target=scheduler._watchdog_loop,
                            args=(my_pid,),
                            name="bursa-watchdog-test", daemon=True)
    wd.start()
    time.sleep(1.8)
    scheduler._WATCHDOG_STOP_EVENT.set()
    wd.join(timeout=3)

    final = get_scheduler_state()
    # owner_pid must remain other_pid — our watchdog must NOT interfere
    assert final.get("owner_pid") == other_pid, (
        f"watchdog must not change owner_pid when it belongs to another "
        f"process, but owner_pid became {final.get('owner_pid')}"
    )
    # cycle_started_at must be untouched
    assert final.get("cycle_started_at") == old_stamp


def test_watchdog_sentinel_owner_causes_stuck_loop_to_self_exit(monkeypatch):
    """
    After the watchdog bumps owner_pid to WATCHDOG_TIMEOUT_OWNER_SENTINEL,
    a running _loop must detect the mismatch on its next iteration and
    exit cleanly. This is the mechanism that prevents thread leaks
    when the watchdog fires.
    """
    import scheduler
    from repository import (get_scheduler_state, update_scheduler_state)
    from db import myt_iso, get_myt_now

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})

    # Reset silent-exit log latch
    if hasattr(scheduler._loop, "_silent_exit_logged"):
        delattr(scheduler._loop, "_silent_exit_logged")

    my_pid = 77777
    # Set DB to show the WATCHDOG sentinel as the new owner
    update_scheduler_state(
        owner_pid=scheduler.WATCHDOG_TIMEOUT_OWNER_SENTINEL,
        running=0,
        kill_switch=0,
        last_heartbeat=myt_iso(get_myt_now() - timedelta(seconds=5)),
    )

    # Spawn a _loop with the OLD pid — should detect mismatch and exit
    scheduler._STOP_EVENT.clear()
    zombie = threading.Thread(target=scheduler._loop, args=(60, my_pid),
                                name="zombie-test", daemon=True)
    zombie.start()
    time.sleep(0.7)
    scheduler._STOP_EVENT.set()
    zombie.join(timeout=3)

    assert not zombie.is_alive(), (
        "_loop must self-exit when owner_pid changes to the watchdog "
        "sentinel — otherwise stuck threads leak forever"
    )
    scheduler._STOP_EVENT.clear()


def test_watchdog_starts_on_scheduler_start_and_stops_on_stop(monkeypatch):
    """
    start() must launch the watchdog; stop() must shut it down.
    Otherwise the watchdog either never runs or never dies.
    """
    import scheduler
    from repository import update_scheduler_state

    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})

    # Clean baseline
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

    ok = scheduler.start(interval_sec=60)
    try:
        assert ok, "start() should return True from clean state"
        # Give the watchdog a moment to be alive
        time.sleep(0.2)
        assert scheduler._WATCHDOG_THREAD is not None, (
            "start() must spawn the watchdog thread"
        )
        assert scheduler._WATCHDOG_THREAD.is_alive(), (
            "watchdog thread must be alive after start()"
        )
    finally:
        scheduler.stop()

    # After stop(), the watchdog handle should be cleared
    assert scheduler._WATCHDOG_THREAD is None, (
        "stop() must tear down the watchdog so a future start() can spawn a new one"
    )
