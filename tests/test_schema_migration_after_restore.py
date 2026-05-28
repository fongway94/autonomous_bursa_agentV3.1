"""
v3.1.13 regression: schema migrations were only run on initial DB
creation, never after a Gist restore. This broke as follows:

  1. User deploys v3.1.10 which adds the ``cycle_started_at`` column
     via ALTER TABLE migration in ``db.init_db()``.
  2. On first boot of the new container, ``init_db()`` correctly adds
     the column.
  3. But ``boot_restore_once()`` then downloads the latest Gist backup
     which was created BEFORE v3.1.10 — old schema without the column.
  4. Restore overwrites the DB file with the old-schema version.
  5. ``init_db()`` is NOT re-run after restore.
  6. First scheduler operation that writes to ``cycle_started_at``
     crashes with ``sqlite3.OperationalError: no such column``.
  7. Every UI button that touches scheduler state crashes.

The fix has two layers:

  A. ``persistence.restore()`` MUST call ``db.init_db()`` after the
     file overwrite to apply any pending migrations.
  B. ``repository.update_scheduler_state()`` MUST degrade gracefully
     when a column is unexpectedly missing — log a warning, drop the
     unknown field, never crash the caller.

Layer A fixes the root cause. Layer B is belt + suspenders so the
agent never hard-crashes again on schema drift.
"""

import os
import sqlite3
import gzip
import base64


def _make_pre_v3_1_10_schema_db(path: str) -> None:
    """
    Build a realistic pre-v3.1.10 DB at ``path`` — has all the columns
    conftest's reset fixture expects, but is MISSING ``cycle_started_at``.
    This mirrors what's in users' Gist backups today.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE scheduler_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            running INTEGER NOT NULL DEFAULT 0,
            interval_sec INTEGER NOT NULL DEFAULT 3600,
            last_run_at TEXT,
            next_run_at TEXT,
            last_heartbeat TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            autotrade_enabled INTEGER NOT NULL DEFAULT 1,
            autoexit_enabled INTEGER NOT NULL DEFAULT 1,
            kill_switch INTEGER NOT NULL DEFAULT 0,
            exploration_mode INTEGER NOT NULL DEFAULT 1,
            exploration_trades_target INTEGER NOT NULL DEFAULT 50,
            owner_pid INTEGER NOT NULL DEFAULT 0
            -- NOTE: cycle_started_at is MISSING (pre-v3.1.10 schema)
        );
        INSERT INTO scheduler_state (id, running) VALUES (1, 0);
    """)
    conn.commit()
    conn.close()


def test_init_db_adds_cycle_started_at_to_existing_old_schema_db(
        tmp_path, monkeypatch):
    """
    Core migration test: if init_db() is called on a DB that already has
    the scheduler_state table but lacks cycle_started_at, it must ADD
    the column via ALTER TABLE.

    This is the contract the v3.1.13 fix relies on.
    """
    # Build an old-schema DB at a temp path and point our connect() at it
    fake_db = tmp_path / "old_schema.db"
    _make_pre_v3_1_10_schema_db(str(fake_db))

    import db
    monkeypatch.setattr(db, "DB_PATH", str(fake_db))

    # Verify precondition
    conn = sqlite3.connect(str(fake_db))
    cols_before = {r[1] for r in conn.execute(
        "PRAGMA table_info(scheduler_state)").fetchall()}
    conn.close()
    assert "cycle_started_at" not in cols_before, (
        "test precondition: old-schema DB must lack the column"
    )

    # Run init_db — should add the missing column
    db.init_db()

    # Verify it was added
    conn = sqlite3.connect(str(fake_db))
    cols_after = {r[1] for r in conn.execute(
        "PRAGMA table_info(scheduler_state)").fetchall()}
    conn.close()
    assert "cycle_started_at" in cols_after, (
        "init_db() must add cycle_started_at via ALTER TABLE — "
        "this is the migration the post-restore fix depends on"
    )


def test_persistence_restore_calls_init_db_after_file_overwrite(
        monkeypatch, tmp_path):
    """
    End-to-end: when persistence.restore() runs, it MUST call init_db()
    AFTER overwriting the local DB file. Otherwise the restored DB has
    stale schema and the next write to a new column crashes.
    """
    import persistence
    import db

    # Track init_db calls in the order they happen
    calls = []
    original_init_db = db.init_db

    def tracker():
        calls.append("init_db")
        return original_init_db()

    monkeypatch.setattr(db, "init_db", tracker)
    if hasattr(persistence, "init_db"):
        monkeypatch.setattr(persistence, "init_db", tracker)

    # Create a fake old-schema DB blob to "restore from"
    old_db_path = tmp_path / "fake_gist.db"
    _make_pre_v3_1_10_schema_db(str(old_db_path))

    with open(old_db_path, "rb") as f:
        encoded = base64.b64encode(gzip.compress(f.read())).decode()

    # Mock the HTTP layer so we don't hit GitHub
    class FakeResponse:
        status_code = 200
        text = encoded
        def json(self):
            return {
                "files": {
                    persistence.GIST_FILENAME: {
                        "content": encoded,
                        "truncated": False,
                    }
                }
            }

    monkeypatch.setattr(persistence, "is_configured", lambda: True)
    monkeypatch.setattr(persistence, "requests",
                         type("M", (), {"get": staticmethod(
                             lambda *a, **k: FakeResponse())}))

    try:
        result = persistence.restore(gist_id="fake_gist_id_for_test")
        assert result.get("ok"), f"restore should succeed: {result}"
        post_restore_init = "init_db" in calls
    finally:
        # Always rebuild a sane schema for subsequent tests
        original_init_db()

    assert post_restore_init, (
        "persistence.restore() MUST call db.init_db() after overwriting "
        "the local DB file. Otherwise stale schemas from old backups "
        "crash the scheduler on the next write."
    )


def test_update_scheduler_state_degrades_gracefully_on_unknown_column():
    """
    Belt + suspenders: even if a column is unexpectedly missing,
    update_scheduler_state() must NOT crash. It should drop the unknown
    column from the SET clause, apply the rest, and (optionally) log
    a warning.

    This is the safety net for any future schema drift we haven't
    anticipated.
    """
    from repository import update_scheduler_state, get_scheduler_state

    # Set a known column to a known value as a control
    update_scheduler_state(running=1)
    assert get_scheduler_state().get("running") == 1

    # Now call with a mix of known + DEFINITELY UNKNOWN column.
    # Before the fix: this raises sqlite3.OperationalError.
    # After the fix: it should succeed for the known column.
    try:
        update_scheduler_state(running=0,
                                _definitely_not_a_real_column="x")
    except Exception as e:
        raise AssertionError(
            f"update_scheduler_state must NOT crash on unknown column. "
            f"Got: {type(e).__name__}: {e}. "
            "The known column update should still apply."
        )

    # Verify the known column update applied
    assert get_scheduler_state().get("running") == 0, (
        "known column update should have succeeded even when an unknown "
        "column was filtered out"
    )


def test_init_db_is_idempotent():
    """
    init_db() must be safely callable multiple times. ALTER TABLE
    ADD COLUMN raises if the column exists — the function must catch
    that. This is the existing contract, but worth pinning down with a
    test since the v3.1.13 fix relies on it.
    """
    from db import init_db, connect

    for i in range(3):
        try:
            init_db()
        except Exception as e:
            raise AssertionError(
                f"init_db() must be idempotent. Call #{i+1} raised: "
                f"{type(e).__name__}: {e}"
            )

    with connect(readonly=True) as c:
        cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(scheduler_state)").fetchall()}
    assert "cycle_started_at" in cols
    assert "owner_pid" in cols
