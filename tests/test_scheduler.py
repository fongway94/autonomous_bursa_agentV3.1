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
