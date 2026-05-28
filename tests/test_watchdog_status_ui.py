"""
v3.1.12 regression tests for ``scheduler.get_watchdog_status()`` —
the UI-facing helper that powers the new Watchdog & Cycle Health
panel in the 🤖 Robo-Trader tab.

The UI must never reach into scheduler module privates (``_WATCHDOG_THREAD``,
``_ORPHANED_THREAD_IDS`` etc.) directly — those are implementation details
that may change. Instead the UI calls this single read-only function and
renders the structured dict it returns.

What the dict must include:
  * watchdog_alive: bool
  * watchdog_thread_ident: int | None
  * cycle_in_flight: bool
  * cycle_running_for_sec: float | None (only if cycle_in_flight)
  * cycle_started_at: str | None
  * recent_timeouts_24h: int  — count of CYCLE_TIMEOUT events in last 24h
  * recent_slow_cycles_24h: int
  * recent_respawns_24h: int
  * orphan_thread_count: int
  * watchdog_timeout_sec: int  — the configured threshold (for UI display)
  * cycle_warn_sec: int

This function MUST be cheap (one SQLite read + a few Python comparisons).
It runs on every Streamlit rerun.
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta


def _reset_scheduler_state(scheduler):
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


def test_get_watchdog_status_returns_required_keys():
    """Schema contract — UI relies on these exact keys."""
    import scheduler
    _reset_scheduler_state(scheduler)
    status = scheduler.get_watchdog_status()

    required = {
        "watchdog_alive",
        "watchdog_thread_ident",
        "cycle_in_flight",
        "cycle_running_for_sec",
        "cycle_started_at",
        "recent_timeouts_24h",
        "recent_slow_cycles_24h",
        "recent_respawns_24h",
        "orphan_thread_count",
        "watchdog_timeout_sec",
        "cycle_warn_sec",
    }
    missing = required - set(status.keys())
    assert not missing, f"get_watchdog_status missing keys: {missing}"


def test_get_watchdog_status_when_watchdog_not_running():
    """Baseline: no watchdog spawned yet → watchdog_alive False, others sane defaults."""
    import scheduler
    _reset_scheduler_state(scheduler)

    status = scheduler.get_watchdog_status()
    assert status["watchdog_alive"] is False
    assert status["watchdog_thread_ident"] is None
    assert status["cycle_in_flight"] is False
    assert status["cycle_running_for_sec"] is None
    assert status["orphan_thread_count"] == 0
    # Tunable knobs surfaced for UI to display
    assert status["watchdog_timeout_sec"] == scheduler.WATCHDOG_CYCLE_TIMEOUT_SEC
    assert status["cycle_warn_sec"] == scheduler.CYCLE_DURATION_WARN_SEC


def test_get_watchdog_status_when_watchdog_alive(monkeypatch):
    """After ensure_watchdog_running, watchdog_alive must be True with valid ident."""
    import scheduler
    _reset_scheduler_state(scheduler)
    try:
        scheduler._ensure_watchdog_running(os.getpid())
        time.sleep(0.1)
        status = scheduler.get_watchdog_status()
        assert status["watchdog_alive"] is True, (
            "watchdog_alive must reflect actual thread liveness"
        )
        assert isinstance(status["watchdog_thread_ident"], int)
        assert status["watchdog_thread_ident"] > 0
    finally:
        scheduler._WATCHDOG_STOP_EVENT.set()
        if scheduler._WATCHDOG_THREAD is not None:
            scheduler._WATCHDOG_THREAD.join(timeout=2)
        scheduler._WATCHDOG_THREAD = None
        scheduler._WATCHDOG_STOP_EVENT.clear()


def test_get_watchdog_status_reports_cycle_in_flight():
    """
    When scheduler_state.cycle_started_at is set, status must report
    cycle_in_flight=True with a sensible cycle_running_for_sec.
    """
    import scheduler
    from repository import update_scheduler_state
    from db import myt_iso, get_myt_now
    _reset_scheduler_state(scheduler)

    # Stamp a cycle as started 17 seconds ago
    stamp = myt_iso(get_myt_now() - timedelta(seconds=17))
    update_scheduler_state(cycle_started_at=stamp)

    status = scheduler.get_watchdog_status()
    assert status["cycle_in_flight"] is True
    assert status["cycle_started_at"] == stamp
    assert status["cycle_running_for_sec"] is not None
    # Should be close to 17s (allow a few seconds for test execution lag)
    assert 15 <= status["cycle_running_for_sec"] <= 30, (
        f"cycle_running_for_sec={status['cycle_running_for_sec']} not "
        "close to expected 17s"
    )


def test_get_watchdog_status_counts_recent_events(monkeypatch):
    """
    The status must summarize recent operational events (CYCLE_TIMEOUT,
    CYCLE_SLOW, WATCHDOG_RESPAWN) from the last 24 hours so the UI can
    show "ops health" at a glance.
    """
    import scheduler
    from logger import log_scheduler_event
    _reset_scheduler_state(scheduler)

    # Insert sample events
    log_scheduler_event("CYCLE_TIMEOUT", "test timeout 1", "ERROR")
    log_scheduler_event("CYCLE_TIMEOUT", "test timeout 2", "ERROR")
    log_scheduler_event("CYCLE_SLOW", "slow cycle 1", "WARN")
    log_scheduler_event("WATCHDOG_RESPAWN", "respawn 1", "WARN")
    # Unrelated events should NOT be counted
    log_scheduler_event("HEARTBEAT", "alive")
    log_scheduler_event("CYCLE_OK", "all good")

    status = scheduler.get_watchdog_status()
    assert status["recent_timeouts_24h"] == 2
    assert status["recent_slow_cycles_24h"] == 1
    assert status["recent_respawns_24h"] == 1


def test_get_watchdog_status_reports_orphan_count():
    """orphan_thread_count must reflect _ORPHANED_THREAD_IDS size."""
    import scheduler
    _reset_scheduler_state(scheduler)

    # Add fake ident
    scheduler._ORPHANED_THREAD_IDS.add(99999)
    scheduler._ORPHANED_THREAD_IDS.add(88888)
    status = scheduler.get_watchdog_status()
    assert status["orphan_thread_count"] == 2

    scheduler._ORPHANED_THREAD_IDS.clear()


def test_get_watchdog_status_is_safe_when_db_unreachable(monkeypatch):
    """
    Must NEVER raise — even if the SQLite query fails, the function
    should return a degraded but valid dict so the UI doesn't crash.
    """
    import scheduler
    _reset_scheduler_state(scheduler)

    # Sabotage the logger call so it raises
    def boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr("logger.get_scheduler_log", boom)

    # Must not raise
    try:
        status = scheduler.get_watchdog_status()
    except Exception as e:
        raise AssertionError(
            f"get_watchdog_status must never raise — got {type(e).__name__}: {e}"
        )

    # The watchdog-thread fields should still be valid; counts default to 0
    assert "watchdog_alive" in status
    assert status["recent_timeouts_24h"] == 0
    assert status["recent_slow_cycles_24h"] == 0
    assert status["recent_respawns_24h"] == 0
