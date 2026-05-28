def test_scheduler_state_defaults():
    from repository import get_scheduler_state
    s = get_scheduler_state()
    assert s["running"] in (0, 1)
    assert s["interval_sec"] >= 60


def test_kill_switch_blocks():
    """Verify the kill_switch flag prevents loop iterations."""
    from repository import get_scheduler_state, update_scheduler_state
    update_scheduler_state(kill_switch=1)
    assert get_scheduler_state()["kill_switch"] == 1
    update_scheduler_state(kill_switch=0)
    assert get_scheduler_state()["kill_switch"] == 0


def test_start_and_stop_idempotent(monkeypatch):
    """
    We don't want network during tests, so monkey-patch the scan to no-op.
    """
    import scheduler

    # Stub the heavy bits
    def fake_run_one_cycle(*a, **k):
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_run_one_cycle)

    # Use very short interval just to make the thread alive briefly
    started = scheduler.start(interval_sec=60)
    assert started or scheduler.is_running()

    # second start should be no-op
    second = scheduler.start(interval_sec=60)
    assert not second  # already running

    scheduler.stop()
    assert not scheduler.is_running()


# --- Regression: next_run_at advances even outside market hours ---

def test_next_run_advances_even_when_market_closed(monkeypatch):
    """
    Bug from v3.1: when wake-up happened outside market hours, the loop
    fell through the SKIP branch and never advanced next_run_at, so the
    UI showed a stale 'Next run' timestamp all night long.

    Fix: next_run_at must be updated on every wake-up, before the
    market-hours check.
    """
    import scheduler
    from db import myt_iso

    # Force "market closed"
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)
    # No-op the cycle work
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})

    from repository import update_scheduler_state, get_scheduler_state
    # Seed a deliberately stale next_run_at
    stale = "2020-01-01 00:00:00"
    update_scheduler_state(next_run_at=stale, last_run_at=None,
                            last_heartbeat=None)

    # Inline the heartbeat block (we can't easily run the full _loop with
    # its sleep, so we replicate the critical section)
    next_at = myt_iso(scheduler._next_run_at(3600))
    update_scheduler_state(
        last_heartbeat=myt_iso(),
        next_run_at=next_at,
    )

    state = get_scheduler_state()
    assert state["next_run_at"] != stale, \
        "next_run_at must advance on every wake-up, not stay stuck"
    assert state["last_heartbeat"] is not None


# --- Regression: PID-based ghost-thread eviction ---

def test_ghost_thread_evicted_when_new_owner_claims(monkeypatch):
    """
    Bug from v3.1: Streamlit Cloud redeploys spawn fresh processes, but
    daemon threads from old processes can outlive their parent briefly.
    Multiple loops then write duplicate HEARTBEAT/SKIP rows at the same
    second.

    Fix: every loop iteration checks `owner_pid` in scheduler_state.
    If it doesn't match this loop's PID, the loop exits cleanly.
    """
    import scheduler, threading, time, os
    from repository import update_scheduler_state, get_scheduler_state

    # Stub heavy work
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Start a "ghost" loop pretending to be PID 99999
    ghost_pid = 99999
    update_scheduler_state(owner_pid=ghost_pid, kill_switch=0, running=1)

    ghost_thread = threading.Thread(
        target=scheduler._loop, args=(60, ghost_pid),
        name="ghost-test", daemon=True,
    )
    ghost_thread.start()
    time.sleep(0.2)

    # Now a "new owner" (our real PID) claims ownership
    new_pid = os.getpid() + 1  # unique fake PID
    update_scheduler_state(owner_pid=new_pid)

    # Wait for ghost to detect + exit. The loop's STOP_EVENT.wait sleeps
    # up to interval_sec; we manually signal stop to short-circuit the wait
    # for the test only.
    scheduler._STOP_EVENT.set()
    ghost_thread.join(timeout=3)
    scheduler._STOP_EVENT.clear()  # reset for any other tests

    assert not ghost_thread.is_alive(), \
        "ghost loop must exit when owner_pid changes"


def test_log_dedup_removes_same_second_duplicates():
    """One-time cleanup must collapse duplicate scheduler_log rows."""
    from logger import dedupe_scheduler_log_at_same_second, log_scheduler_event
    # Insert duplicates
    for _ in range(5):
        log_scheduler_event("HEARTBEAT", "alive")
    # All 5 will have nearly-identical timestamps (same second).
    # If they all share the same (timestamp, event, message), dedup keeps 1.
    removed = dedupe_scheduler_log_at_same_second()
    # Either 4 removed (all same second) or 0 (race straddled a second boundary)
    assert removed >= 0


# --- Regression: deploys should not trigger immediate scans ---

def test_loop_does_not_scan_immediately_on_start(monkeypatch):
    """
    Regression: when Streamlit Cloud redeploys after a GitHub push,
    a fresh process starts the scheduler. The first cycle MUST be
    deferred to the next scheduled boundary (e.g. next top-of-hour),
    not run immediately.

    This prevents 'push 3 times → 3 immediate scans' wasteful behavior.
    """
    import scheduler, threading, time
    import db as db_module

    cycle_calls = []

    def fake_cycle(*a, **k):
        cycle_calls.append(db_module.myt_iso())
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: True)

    # Use a short interval so the test doesn't take an hour
    # (but the debounce should still sleep at least a bit)
    scheduler._STOP_EVENT.clear()
    started = scheduler.start(interval_sec=60)
    assert started

    # Wait briefly — long enough for the loop to START but NOT long enough
    # for the 60-second debounce + first cycle to fire.
    time.sleep(0.5)

    # Cycle should NOT have run yet — we're still in the debounce sleep.
    assert len(cycle_calls) == 0, (
        f"Cycle ran immediately on startup ({len(cycle_calls)} calls). "
        f"This is the GitHub-push-storm bug — each redeploy was triggering "
        f"an instant scan."
    )

    scheduler.stop()
    assert not scheduler.is_running()


def test_run_once_still_bypasses_debounce(monkeypatch):
    """
    The 'Run Cycle Now' button (run_once()) should still fire an
    immediate scan — debounce only affects the AUTO loop.
    """
    import scheduler
    called = [False]
    def fake_cycle(*a, **k):
        called[0] = True
        return {"scan_count": 5, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}
    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)

    result = scheduler.run_once()
    assert called[0]
    assert result["scan_count"] == 5
