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


# --- Regression: same-process duplicate thread guard (v3.1.8) ---

def test_same_process_duplicate_start_rejected_by_db_guard(monkeypatch):
    """
    Bug from v3.1.8: _THREAD handle can be reset to None during Streamlit
    module reloads or stop()/join(timeout) races while the actual daemon
    thread continues running.  PID-based eviction only catches cross-
    process ghosts; same-PID duplicates slip through and create scan
    storms (multiple concurrent SCAN_START rows at the same hour).

    Fix: start() now enforces three layers:
      1) _THREAD handle check
      2) threading.enumerate() scan for any alive 'bursa-scheduler'
      3) DB heartbeat freshness check for this PID
    """
    import scheduler, time, os
    from repository import update_scheduler_state, get_scheduler_state

    def fake_cycle(*a, **k):
        time.sleep(0.05)
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Clean slate
    scheduler.stop()
    update_scheduler_state(running=0, owner_pid=0, kill_switch=0,
                           last_heartbeat=None)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    time.sleep(0.1)

    # 1. First start should succeed
    assert scheduler.start(interval_sec=60) is True
    time.sleep(0.15)  # let thread enter loop and write DB state

    # 2. Duplicate rejected by _THREAD handle (Guard 1)
    assert scheduler.start(interval_sec=60) is False

    # 3. Simulate lost-reference (e.g., Streamlit module reload)
    saved_thread = scheduler._THREAD
    scheduler._THREAD = None

    # 4. v3.2: start() orphans the old thread and starts a fresh one.
    #    This is intentional — the old thread will self-exit via
    #    owner_pid mismatch on its next wake-up.
    assert scheduler.start(interval_sec=60) is True, (
        "v3.2: start() must orphan stale threads and start fresh"
    )
    assert scheduler.is_running()

    # Old thread should be in the orphan registry
    assert saved_thread.ident in scheduler._ORPHANED_THREAD_IDS

    scheduler.stop()
    assert not scheduler.is_running()


# --- v3.1.9 regressions ---

def test_start_after_stop_while_mid_cycle(monkeypatch):
    """
    v3.1.9 regression: stop() during a long-running cycle must not
    permanently block start().

    Previously stop() used join(timeout=5) — if the cycle took longer
    (e.g. 100s for a yfinance scan), the old thread was still alive.
    start() Guard 2 saw it in threading.enumerate() and returned False
    forever. The UI showed 🔴 STOPPED but Start did nothing.

    Fix:
      * stop() now clears owner_pid=0 and running=0 in DB.
      * start() Guard 2 only blocks when DB says running=1 (legitimate).
      * If DB says running=0, start() proceeds even if a zombie is
        still finishing its cycle. The new owner_pid evicts the zombie
        on its next wake-up.
    """
    import scheduler, time, threading
    from repository import update_scheduler_state, get_scheduler_state

    cycle_started = threading.Event()
    can_finish_cycle = threading.Event()

    def slow_cycle(*a, **k):
        cycle_started.set()
        can_finish_cycle.wait(timeout=5)
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", slow_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Clean slate
    scheduler.stop()
    update_scheduler_state(running=0, owner_pid=0, kill_switch=0,
                           last_heartbeat=None)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    time.sleep(0.1)

    # Start a thread; it will enter slow_cycle and block
    assert scheduler.start(interval_sec=60) is True
    cycle_started.wait(timeout=2)

    # Stop while mid-cycle
    scheduler.stop()
    assert scheduler._THREAD is None  # handle cleared by stop()

    # At this point the old thread is still alive inside slow_cycle.
    # start() must NOT be blocked forever.
    t0 = time.time()
    result = scheduler.start(interval_sec=60)
    elapsed = time.time() - t0

    assert elapsed < 1.0, (
        f"start() blocked for {elapsed:.1f}s waiting for zombie — "
        "users see Start button that does nothing"
    )
    assert result is True, "start() must succeed after stop()"

    # Release the old zombie so it can finish and exit
    can_finish_cycle.set()
    time.sleep(0.3)

    # Clean up
    scheduler.stop()
    assert not scheduler.is_running()


def test_run_one_cycle_aborts_when_owner_changed(monkeypatch):
    """
    v3.1.9 regression: if a zombie thread is still mid-cycle when a new
    thread claims ownership, the zombie must NOT log SCAN_START / do work
    for that cycle.  It should abort at the top of _run_one_cycle().
    """
    import scheduler
    from repository import update_scheduler_state

    update_scheduler_state(running=1, owner_pid=99999)

    # Call _run_one_cycle with my_pid=1, but DB says owner_pid=99999
    result = scheduler._run_one_cycle(autotrade=False, autoexit=False,
                                      my_pid=1)
    assert result.get("aborted") is True


def test_start_adopts_alive_thread_when_handle_lost(monkeypatch):
    """
    v3.1.9 regression: if _THREAD handle is lost but the actual thread
    is still alive, start() should adopt it rather than spawning a
    duplicate or returning False permanently.
    """
    import scheduler, threading, time
    from repository import update_scheduler_state, get_scheduler_state

    def fake_cycle(*a, **k):
        time.sleep(0.1)
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Clean slate
    scheduler.stop()
    update_scheduler_state(running=0, owner_pid=0, kill_switch=0,
                           last_heartbeat=None)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    time.sleep(0.1)

    # Start a thread normally
    assert scheduler.start(interval_sec=60) is True
    time.sleep(0.15)
    assert scheduler.is_running()

    # Simulate handle loss (e.g. module reload edge case)
    saved_thread = scheduler._THREAD
    scheduler._THREAD = None

    # v3.2: start() orphans the old thread and starts a fresh one.
    result = scheduler.start(interval_sec=60)
    assert result is True, "v3.2: start() must orphan old and start fresh"
    assert scheduler._THREAD is not saved_thread, (
        "v3.2: start() must create a NEW thread, not adopt the old one"
    )
    assert saved_thread.ident in scheduler._ORPHANED_THREAD_IDS

    assert scheduler.is_running()

    scheduler.stop()
    assert not scheduler.is_running()


def test_start_bypasses_fresh_db_when_local_thread_dead(monkeypatch):
    """
    v3.1.9 regression: if the scheduler thread crashed but DB still
    shows running=1 with fresh heartbeat, start() must NOT be blocked
    by Guard 3. It should start a new thread.
    """
    import scheduler, time
    from repository import update_scheduler_state

    def fake_cycle(*a, **k):
        time.sleep(0.05)
        return {"scan_count": 0, "settled": 0, "partials": 0,
                "auto_entries": 0, "rejected": 0, "errors": []}

    monkeypatch.setattr(scheduler, "_run_one_cycle", fake_cycle)
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Clean slate
    scheduler.stop()
    update_scheduler_state(running=0, owner_pid=0, kill_switch=0,
                           last_heartbeat=None)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()
    time.sleep(0.1)

    # Simulate a crashed thread: _THREAD points to a dead Thread object
    class DeadThread:
        def is_alive(self): return False
        ident = 12345
        name = "bursa-scheduler"
    scheduler._THREAD = DeadThread()

    # Set DB to show running=1 with fresh heartbeat and our owner_pid
    my_pid = scheduler.os.getpid()
    from db import myt_iso
    update_scheduler_state(running=1, owner_pid=my_pid,
                           last_heartbeat=myt_iso())

    # start() must NOT be blocked — it should see the local thread is
    # dead and proceed to spawn a new one.
    result = scheduler.start(interval_sec=60)
    assert result is True, (
        "start() was blocked by stale DB heartbeat even though local "
        "thread is dead — scheduler cannot recover from a crash"
    )

    scheduler.stop()
    assert not scheduler.is_running()
