"""
Regression tests for v3.1.1 daily-maintenance idempotency.
"""

import threading


def test_claim_daily_task_first_caller_wins():
    from repository import try_claim_daily_task
    assert try_claim_daily_task("test_task_A", owner_pid=111) is True
    assert try_claim_daily_task("test_task_A", owner_pid=222) is False
    assert try_claim_daily_task("test_task_A", owner_pid=333) is False


def test_different_tasks_dont_collide():
    from repository import try_claim_daily_task
    assert try_claim_daily_task("test_task_X", owner_pid=1) is True
    assert try_claim_daily_task("test_task_Y", owner_pid=1) is True
    assert try_claim_daily_task("test_task_X", owner_pid=1) is False


def test_claim_records_owner_pid_and_timestamp():
    from repository import try_claim_daily_task
    from db import connect
    try_claim_daily_task("test_task_Z", owner_pid=42)
    with connect(readonly=True) as c:
        row = c.execute(
            "SELECT * FROM maintenance_state WHERE task_name=?",
            ("test_task_Z",),
        ).fetchone()
    assert row is not None
    assert row["owner_pid"] == 42
    assert row["last_ran_date"]
    assert row["last_ran_at"]


def test_result_recorded():
    from repository import try_claim_daily_task, record_daily_task_result
    from db import connect
    try_claim_daily_task("test_result_task", owner_pid=99)
    record_daily_task_result("test_result_task", "oos_acc=0.731")
    with connect(readonly=True) as c:
        row = c.execute(
            "SELECT result FROM maintenance_state WHERE task_name=?",
            ("test_result_task",),
        ).fetchone()
    assert row["result"] == "oos_acc=0.731"


def test_concurrent_claims_only_one_winner():
    """
    The KEY test — 20 concurrent threads racing to claim the same
    task. Exactly ONE must win. This proves the SQL CAS works.
    """
    from repository import try_claim_daily_task
    results = []
    lock = threading.Lock()

    def claim(tid):
        won = try_claim_daily_task("race_task", owner_pid=tid)
        with lock:
            results.append((tid, won))

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [tid for tid, won in results if won]
    assert len(winners) == 1, (
        f"Exactly 1 thread should win the daily-task claim, got {len(winners)}: "
        f"{winners}."
    )


def test_dedup_collapses_daily_event_multiplications():
    from logger import (
        log_scheduler_event, dedupe_scheduler_log_at_same_second,
        get_scheduler_log,
    )
    for acc in [0.630, 0.638, 0.645, 0.639, 0.631, 0.640, 0.637, 0.644]:
        log_scheduler_event(
            "NIGHTLY_RETRAIN",
            f"ML classifier retrained, OOS acc={acc:.3f}",
        )
    pre = len([l for l in get_scheduler_log(200)
               if l["event"] == "NIGHTLY_RETRAIN"])
    assert pre == 8

    removed = dedupe_scheduler_log_at_same_second()
    assert removed >= 7

    post = len([l for l in get_scheduler_log(200)
                if l["event"] == "NIGHTLY_RETRAIN"])
    assert post == 1
