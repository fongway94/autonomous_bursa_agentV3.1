"""
Tests for v3.1.6 ML classifier persistence + auto-train.

Two changes are tested:
  1. The ML classifier .pkl is now included in the Gist backup payload
     (so it survives container resets just like the DB)
  2. _encode_ml_for_gist gracefully returns None when no .pkl exists yet
"""

import os


def test_encode_ml_returns_none_when_no_pkl(monkeypatch, tmp_path):
    """If the .pkl doesn't exist, encoder must return None (not crash)."""
    import persistence
    monkeypatch.setattr(persistence, "ML_MODEL_PATH",
                        str(tmp_path / "nonexistent.pkl"))
    assert persistence._encode_ml_for_gist() is None


def test_encode_ml_roundtrip(tmp_path, monkeypatch):
    """gzip+b64 must preserve every byte of the .pkl exactly."""
    import persistence

    # Create a fake .pkl with known content (joblib files start with bytes
    # that look like ZIP/pickle; we just need any binary blob for the test)
    fake_pkl = b"\x80\x04\x95" + b"scikit-learn model bytes" * 100 + bytes(range(256))
    fake_path = tmp_path / "fake_model.pkl"
    fake_path.write_bytes(fake_pkl)

    monkeypatch.setattr(persistence, "ML_MODEL_PATH", str(fake_path))

    encoded = persistence._encode_ml_for_gist()
    assert encoded is not None
    assert isinstance(encoded, str)
    assert len(encoded) > 0

    # Restore to a different path
    restored = tmp_path / "restored.pkl"
    n = persistence._decode_gist_to_db(encoded, str(restored))
    assert n == len(fake_pkl)
    assert restored.read_bytes() == fake_pkl


def test_backup_payload_includes_ml_file_when_present(monkeypatch, tmp_path):
    """
    When .pkl exists, backup() must upload BOTH files to the gist.
    We mock the HTTP call and inspect the payload.
    """
    import persistence

    # Create a fake .pkl
    ml_path = tmp_path / "model.pkl"
    ml_path.write_bytes(b"fake_ml_model_bytes" * 50)
    monkeypatch.setattr(persistence, "ML_MODEL_PATH", str(ml_path))

    # Fake credentials
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")

    # Reset rate limit + marker for clean POST path
    persistence._last_backup_ts = None
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))

    captured = {}

    class FakeResp:
        status_code = 201
        def json(self):
            return {"id": "fake_gist_id_123"}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return FakeResp()

    monkeypatch.setattr(persistence.requests, "post", fake_post)
    # Marker file is empty, so it'll go down the POST (create) path

    result = persistence.backup(force=True, reason="test")
    assert result["ok"] is True

    # Verify BOTH files in the payload
    files = captured["payload"]["files"]
    assert persistence.GIST_FILENAME in files
    assert persistence.ML_GIST_FILENAME in files

    # Description mentions both sizes
    desc = captured["payload"]["description"]
    assert "DB:" in desc
    assert "ML:" in desc


def test_backup_payload_excludes_ml_when_no_pkl(monkeypatch, tmp_path):
    """If no .pkl yet, backup includes only the DB file."""
    import persistence

    # Point at non-existent .pkl
    monkeypatch.setattr(persistence, "ML_MODEL_PATH",
                        str(tmp_path / "nothing.pkl"))
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    persistence._last_backup_ts = None
    # Reset marker so backup() goes the POST (create) path consistently
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))

    captured = {}

    class FakeResp:
        status_code = 201
        def json(self):
            return {"id": "fake_id"}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return FakeResp()

    monkeypatch.setattr(persistence.requests, "post", fake_post)

    result = persistence.backup(force=True, reason="test-no-ml")
    assert result["ok"] is True

    files = captured["payload"]["files"]
    assert persistence.GIST_FILENAME in files
    assert persistence.ML_GIST_FILENAME not in files


def test_restore_handles_gist_without_ml_file(monkeypatch, tmp_path):
    """
    Older gists (created before v3.1.6) don't have the ML file.
    Restore must still succeed, just with ml_bytes_restored=0.
    """
    import persistence
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")

    # Pre-set the gist_id in marker
    persistence._write_marker({"gist_id": "old_gist_id"})

    # Build a minimal valid encoded DB content
    fake_db = b"SQLite format 3\x00" + b"\x00" * 1000
    import gzip, base64
    encoded_db = base64.b64encode(
        gzip.compress(fake_db)).decode("ascii")

    # Mock the GitHub API: returns gist WITHOUT the ML file
    class FakeResp:
        status_code = 200
        def json(self):
            return {
                "id": "old_gist_id",
                "files": {
                    persistence.GIST_FILENAME: {
                        "content": encoded_db,
                        "truncated": False,
                    },
                },
            }

    def fake_get(url, headers=None, timeout=None):
        return FakeResp()

    monkeypatch.setattr(persistence.requests, "get", fake_get)

    # Use temp paths so we don't trash real data
    monkeypatch.setattr(persistence, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(persistence, "ML_MODEL_PATH", str(tmp_path / "test.pkl"))

    result = persistence.restore()
    assert result["ok"] is True
    assert result["bytes_restored"] > 0
    assert result.get("ml_bytes_restored", 0) == 0  # no ML in old gist
    assert "DB only" in result["reason"]


def test_ml_constants_defined():
    """Sanity: the new v3.1.6 constants exist on persistence module."""
    import persistence
    assert hasattr(persistence, "ML_GIST_FILENAME")
    assert hasattr(persistence, "ML_MODEL_PATH")
    assert persistence.ML_GIST_FILENAME == "setup_classifier.pkl.b64.gz"
    assert "setup_classifier.pkl" in persistence.ML_MODEL_PATH
