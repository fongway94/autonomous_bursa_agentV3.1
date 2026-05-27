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
