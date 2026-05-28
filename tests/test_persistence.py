"""
Tests for v3.1.5 Gist-backed persistence layer.

The persistence module solves the critical problem where Streamlit Cloud
container resets (on every GitHub push, manual reboot, 7-day sleep, or
platform maintenance) would wipe the entire SQLite database — including
trade history, Bayesian brain (state_priors), biases, parameters.

These tests verify:
  * No-op safety: works (degrades silently) when GITHUB_TOKEN missing
  * Encode round-trip: gzip+b64 preserves DB bytes exactly
  * Marker file is read/written correctly
  * Boot-restore skips when local DB already has data
  * Rate-limit prevents backup storms
"""

import os


def test_unconfigured_status():
    """Without GITHUB_TOKEN, is_configured returns False."""
    from persistence import is_configured
    if "GITHUB_TOKEN" not in os.environ:
        assert is_configured() is False


def test_unconfigured_backup_returns_error_not_raise(monkeypatch):
    """backup() must never raise even without credentials."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from persistence import backup
    result = backup(force=True, reason="test")
    assert result["ok"] is False
    assert "GITHUB_TOKEN" in result["reason"]


def test_unconfigured_restore_returns_error_not_raise(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from persistence import restore
    result = restore()
    assert result["ok"] is False
    assert "GITHUB_TOKEN" in result["reason"]


def test_status_dict_shape(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from persistence import get_status
    s = get_status()
    assert "configured" in s
    assert "db_size_kb" in s
    assert s["configured"] is False
    assert isinstance(s["db_size_kb"], (int, float))


def test_encode_decode_roundtrip(tmp_path):
    """gzip+b64 encoding must preserve every byte exactly."""
    from persistence import _encode_db_for_gist, _decode_gist_to_db
    import persistence

    # Create a fake DB with known content
    fake_db_content = b"SQLite format 3\x00" + b"x" * 1000 + bytes(range(256))
    fake_db_path = tmp_path / "fake.db"
    fake_db_path.write_bytes(fake_db_content)

    # Monkey-patch the DB_PATH
    original = persistence.DB_PATH
    persistence.DB_PATH = str(fake_db_path)

    try:
        encoded = _encode_db_for_gist()
        assert isinstance(encoded, str)
        assert len(encoded) > 0

        # Restore to a different path
        restored_path = tmp_path / "restored.db"
        n = _decode_gist_to_db(encoded, str(restored_path))
        assert n == len(fake_db_content)
        assert restored_path.read_bytes() == fake_db_content
    finally:
        persistence.DB_PATH = original


def test_marker_read_write_round_trip(tmp_path, monkeypatch):
    """Marker file should round-trip a dict."""
    import persistence
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))

    # Empty when missing
    assert persistence._read_marker() == {}

    # Write + re-read
    persistence._write_marker({"gist_id": "abc123",
                                "last_backup_at": "2026-05-28 10:00:00"})
    m = persistence._read_marker()
    assert m["gist_id"] == "abc123"
    assert m["last_backup_at"] == "2026-05-28 10:00:00"


def test_boot_restore_skips_when_local_db_has_data(monkeypatch):
    """
    Critical safety guarantee: if local DB already has trade data,
    boot_restore_once must NOT overwrite it with the gist version.
    Otherwise repeated reboots would wipe today's trades.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token_for_test")

    # Reset the once-flag so this test isn't affected by other tests
    import persistence
    persistence._BOOT_RESTORE_ATTEMPTED = False

    # Insert a trade so the local DB has data
    from repository import insert_trade
    insert_trade({
        "ticker": "TEST.KL", "name": "T", "sector": "Tech",
        "signal_type": "GOLD BUY (BREAKOUT)",
        "entry_price": 1.0, "stop_loss": 0.9,
        "tp1": 1.1, "tp2": 1.2, "tp3": 1.3,
        "shares": 100, "lots": 1, "cost": 100, "fee": 0.15,
        "total_outlay": 100.15, "risk_per_share": 0.1,
        "actual_risk_pct": 10, "status": "ACTIVE", "phase": "FULL",
        "logged_at": "2026-05-28 09:00:00",
        "shares_remaining": 100,
    })

    result = persistence.boot_restore_once()
    assert result["skipped"] is True
    assert "trades" in result["reason"].lower() \
        or "priors" in result["reason"].lower()


def test_boot_restore_idempotent_per_process(monkeypatch):
    """boot_restore_once should only attempt once per Python process."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token_for_test")
    import persistence
    persistence._BOOT_RESTORE_ATTEMPTED = False

    # First call — could succeed/fail/skip, but flag becomes True
    persistence.boot_restore_once()
    assert persistence._BOOT_RESTORE_ATTEMPTED is True

    # Second call must skip with "already attempted"
    r = persistence.boot_restore_once()
    assert r["skipped"] is True
    assert "already attempted" in r["reason"]


def test_backup_rate_limit(monkeypatch):
    """
    Without rate-limit, the agent could hammer GitHub API on every
    scheduler tick + every trade event. Rate-limit ensures min 30s gap.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token_for_test")
    import persistence
    from datetime import datetime, timezone, timedelta

    # Fake a recent backup
    persistence._last_backup_ts = datetime.now(
        timezone(timedelta(hours=8)))

    # Immediate retry should be rate-limited (skipped)
    result = persistence.backup(force=False, reason="too-soon")
    assert result["skipped"] is True
    assert "rate-limited" in result["reason"]


def test_backup_force_bypasses_rate_limit(monkeypatch):
    """force=True (e.g. manual button) bypasses the rate limit."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # ensure fail-fast path
    import persistence
    from datetime import datetime, timezone, timedelta

    persistence._last_backup_ts = datetime.now(
        timezone(timedelta(hours=8)))

    # force=True should NOT be rate-limited — but will fail
    # on the missing token (proving it got past rate-limit check)
    result = persistence.backup(force=True, reason="manual")
    assert result.get("skipped", False) is False
    # The actual failure is missing token, not rate-limit
    assert "GITHUB_TOKEN" in result["reason"]
