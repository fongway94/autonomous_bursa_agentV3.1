"""
v3.1.8 regression: duplicate worker loops were causing log spam like
"10 SKIPs in 16 seconds" because multiple Streamlit reruns each spawned
a fresh scheduler thread, and each thread logged HEARTBEAT/SKIP before
realizing it was a ghost.

Fix: be conservative about spawning new loops + check ownership FIRST
with heartbeat freshness, exit silently if a live owner exists.
"""

import os
import time
from datetime import datetime, timezone, timedelta


def test_ensure_started_does_not_spawn_when_other_owner_alive(monkeypatch):
    """
    If another process is the registered owner and its heartbeat is
    recent (<5 min), ensure_started must do NOTHING — no new thread,
    no force_restart.
    """
    import scheduler
    from repository import update_scheduler_state
    from db import myt_iso, get_myt_now

    # Stub heavy work
    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Make sure no local thread is running
    scheduler._STOP_EVENT.set()
    if scheduler._THREAD is not None:
        scheduler._THREAD.join(timeout=2)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()

    # Simulate another live owner with a fresh heartbeat (5 sec ago)
    fake_other_pid = 99999
    fake_recent_hb = myt_iso(get_myt_now() - timedelta(seconds=5))
    update_scheduler_state(owner_pid=fake_other_pid,
                            last_heartbeat=fake_recent_hb,
                            running=1, kill_switch=0)

    # ensure_started should NOT spawn a thread
    scheduler.ensure_started(interval_sec=60)

    # Verify no local thread was created
    assert scheduler._THREAD is None or not scheduler._THREAD.is_alive(), \
        "ensure_started must NOT spawn a thread when another live owner exists"


def test_ensure_started_takes_over_when_owner_is_stale(monkeypatch):
    """If the registered owner hasn't beat in >5 min, we can take over."""
    import scheduler
    from repository import update_scheduler_state
    from db import myt_iso, get_myt_now

    scheduler._STOP_EVENT.set()
    if scheduler._THREAD is not None:
        scheduler._THREAD.join(timeout=2)
    scheduler._THREAD = None
    scheduler._STOP_EVENT.clear()

    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Simulate stale owner — last beat 10 minutes ago
    fake_other_pid = 99999
    fake_stale_hb = myt_iso(get_myt_now() - timedelta(minutes=10))
    update_scheduler_state(owner_pid=fake_other_pid,
                            last_heartbeat=fake_stale_hb,
                            running=1, kill_switch=0)

    scheduler.ensure_started(interval_sec=60)
    time.sleep(0.3)
    assert scheduler._THREAD is not None and scheduler._THREAD.is_alive(), \
        "ensure_started must spawn when previous owner is stale"

    scheduler.stop()


def test_loop_exits_silently_when_other_live_owner_exists(monkeypatch):
    """
    Critical regression: a duplicate loop must exit WITHOUT logging
    HEARTBEAT or SKIP. Otherwise we get the 10-SKIPs-in-16-seconds spam.
    """
    import scheduler, threading
    from repository import update_scheduler_state, get_scheduler_state
    from db import myt_iso, get_myt_now
    from logger import get_scheduler_log

    monkeypatch.setattr(scheduler, "_run_one_cycle",
                        lambda *a, **k: {"scan_count": 0, "settled": 0,
                                          "partials": 0, "auto_entries": 0,
                                          "rejected": 0, "errors": []})
    monkeypatch.setattr(scheduler, "_is_market_hours", lambda: False)

    # Pre-set a different LIVE owner with very recent heartbeat
    fake_live_pid = 88888
    fake_recent_hb = myt_iso(get_myt_now() - timedelta(seconds=10))
    update_scheduler_state(owner_pid=fake_live_pid,
                            last_heartbeat=fake_recent_hb,
                            running=1, kill_switch=0)

    # Snapshot log row count
    rows_before = len(get_scheduler_log(limit=100))

    # Start a "duplicate" loop with a different PID
    ghost_pid = 77777
    scheduler._STOP_EVENT.clear()
    setattr(scheduler._loop, "_silent_exit_logged", False)  # reset
    ghost_thread = threading.Thread(
        target=scheduler._loop, args=(60, ghost_pid),
        name="dup-test", daemon=True)
    ghost_thread.start()

    # Give it a moment to wake, check ownership, exit
    time.sleep(0.5)
    scheduler._STOP_EVENT.set()
    ghost_thread.join(timeout=3)
    scheduler._STOP_EVENT.clear()

    rows_after = len(get_scheduler_log(limit=200))

    # The ghost should have written AT MOST 2 rows (STARTED + GHOST_EXIT)
    # NOT a HEARTBEAT or SKIP row.
    new_rows = rows_after - rows_before
    skip_rows = [r for r in get_scheduler_log(50)
                  if r["event"] == "SKIP"
                  and f"PID {ghost_pid}" in (r.get("message") or "")]
    heartbeat_rows = [r for r in get_scheduler_log(50)
                       if r["event"] == "HEARTBEAT"
                       and f"PID {ghost_pid}" in (r.get("message") or "")]
    assert len(skip_rows) == 0, (
        f"duplicate loop must NOT log SKIP — found {len(skip_rows)} rows")
    assert len(heartbeat_rows) == 0, (
        f"duplicate loop must NOT log HEARTBEAT — found {len(heartbeat_rows)}")


def test_per_minute_dedup_collapses_skip_storm():
    """
    The historical-data cleanup must collapse 10 SKIPs in the same
    minute down to 1.
    """
    from logger import (log_scheduler_event,
                        dedupe_scheduler_log_within_minute,
                        get_scheduler_log)

    # Simulate the bug: 10 SKIPs within the same minute
    for i in range(10):
        log_scheduler_event(
            "SKIP",
            f"Outside market hours — PRE_OPEN_PM session (14:00–14:30) "
            f"(next: 14:30) PID {7000 + i}")

    skip_before = sum(1 for r in get_scheduler_log(200)
                       if r["event"] == "SKIP")
    assert skip_before == 10

    removed = dedupe_scheduler_log_within_minute(["SKIP"])
    assert removed >= 9

    skip_after = sum(1 for r in get_scheduler_log(200)
                      if r["event"] == "SKIP")
    assert skip_after == 1, (
        f"per-minute dedup should leave 1 SKIP row, got {skip_after}")


def test_per_minute_dedup_preserves_different_minutes():
    """Dedup must NOT collapse events from different minutes."""
    from logger import (dedupe_scheduler_log_within_minute,
                        get_scheduler_log)
    from db import connect, myt_iso

    # Insert SKIPs in 3 different minutes manually
    with connect() as c:
        for minute in [0, 1, 2]:
            for i in range(3):
                ts = f"2026-05-28 14:0{minute}:{i:02d}"
                c.execute(
                    "INSERT INTO scheduler_log "
                    "(timestamp, level, event, message) "
                    "VALUES (?,?,?,?)",
                    (ts, "INFO", "SKIP", f"test-{minute}-{i}"))

    removed = dedupe_scheduler_log_within_minute(["SKIP"])
    # 3 minutes × 3 SKIPs each = 9, should collapse to 3 (1 per minute)
    remaining = sum(1 for r in get_scheduler_log(200)
                     if r["event"] == "SKIP")
    assert remaining == 3, f"should keep 3 rows (1 per minute), got {remaining}"
