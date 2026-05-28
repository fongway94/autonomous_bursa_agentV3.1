# persistence.py
"""
Persistent backup of the agent's SQLite database to a private GitHub Gist.

Solves the v3.1 problem where Streamlit Cloud container resets (caused by
GitHub pushes, manual reboots, 7-day sleep, or platform maintenance) wiped
the agent's learning data — including state_priors (Bayesian brain),
trade history, account balance, biases, parameters, etc.

Design
------
* A single private Gist holds the latest copy of `bursa_agent.db`.
* On boot, the agent downloads the latest backup BEFORE the scheduler
  starts (so the brain is restored before any cycle runs).
* Backups fire on:
    1. Every closed trade (so brain learning is preserved instantly)
    2. Every hourly scheduler heartbeat (safety net)
    3. Daily maintenance (consolidation)
* Old gist revisions are kept by GitHub forever (free), so you have
  rollback history without doing anything.

Credentials
-----------
Requires ONE secret in Streamlit Cloud → Manage app → Secrets:

  GITHUB_TOKEN = "ghp_..."
  # Personal Access Token (CLASSIC, not fine-grained) with scope: gist.
  # Generate at https://github.com/settings/tokens (NOT ?type=beta)

Optionally, also set GIST_ID if you want to survive container resets
without relying on the local marker file:

  GIST_ID = "your-gist-id-here"

The first backup will create the gist; subsequent backups update the
same gist (we remember its ID in a tiny marker file inside the data dir,
which itself is also backed up).  v3.1.9 ALSO stores the gist_id in the
SQLite `meta` table so it survives container resets via the Gist backup.

Safety guarantees
-----------------
* All backup/restore code is wrapped in try/except — never crashes the agent.
* If GITHUB_TOKEN is missing, the module degrades silently (status shown
  in UI as "❌ not configured" but app still works).
* DB file is compressed with gzip before upload (typically 4-10x smaller).
* gzip + base64-encoded for Gist storage (Gists store text).
* v3.1.6: ML classifier .pkl is also backed up alongside the DB.
* v3.1.9: gist_id is stored in SQLite `meta` table (survives restore)
  and GIST_ID env var is used as fallback when local marker is lost.
"""

from __future__ import annotations
import os
import gzip
import base64
import json
import threading
from datetime import datetime, timezone, timedelta

import requests

from db import DATA_DIR, DB_PATH, get_meta, set_meta, myt_iso
from logger import get_logger

log = get_logger("persistence")

MYT = timezone(timedelta(hours=8))
GIST_API = "https://api.github.com/gists"
GIST_FILENAME = "bursa_agent_db.b64.gz"
ML_GIST_FILENAME = "setup_classifier.pkl.b64.gz"  # v3.1.6: ML model
MARKER_FILE = os.path.join(DATA_DIR, ".gist_marker.json")
ML_MODEL_PATH = os.path.join(DATA_DIR, "setup_classifier.pkl")

# Avoid overlapping backups
_BACKUP_LOCK = threading.RLock()

# Avoid uploading more often than this many seconds even if called rapidly
MIN_BACKUP_INTERVAL_SEC = 30


# ---------------------------------------------------------------------------
# Credentials + marker
# ---------------------------------------------------------------------------

def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN")


def is_configured() -> bool:
    return bool(_token())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# v3.1.9: read marker from DB meta first (survives container reset + restore),
# then fall back to local JSON file for backwards compatibility.
def _read_marker() -> dict:
    # Primary: DB meta
    try:
        raw = get_meta("gist_marker")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # Fallback: local file
    if not os.path.exists(MARKER_FILE):
        return {}
    try:
        with open(MARKER_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# v3.1.9: write to DB meta (backed up in Gist) AND local file.
def _write_marker(data: dict) -> None:
    try:
        set_meta("gist_marker", json.dumps(data))
    except Exception as e:
        log.warning(f"DB meta marker write failed: {e}")
    try:
        with open(MARKER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"file marker write failed: {e}")


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------

def _encode_db_for_gist() -> str:
    """Read the SQLite DB, gzip + base64-encode for storage in a text Gist."""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"DB not found at {DB_PATH}")
    with open(DB_PATH, "rb") as f:
        raw = f.read()
    compressed = gzip.compress(raw, compresslevel=6)
    encoded = base64.b64encode(compressed).decode("ascii")
    return encoded


def _decode_gist_to_db(encoded: str, target_path: str) -> int:
    """Reverse of _encode_db_for_gist. Returns bytes written."""
    compressed = base64.b64decode(encoded.encode("ascii"))
    raw = gzip.decompress(compressed)
    # Write atomically — temp file then rename, so a half-written DB never
    # appears.
    tmp_path = target_path + ".restoring"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, target_path)
    return len(raw)


def _encode_ml_for_gist() -> str | None:
    """
    Encode the ML classifier .pkl for gist storage.
    Returns None if no .pkl exists (no model to back up yet).
    """
    if not os.path.exists(ML_MODEL_PATH):
        return None
    with open(ML_MODEL_PATH, "rb") as f:
        raw = f.read()
    compressed = gzip.compress(raw, compresslevel=6)
    return base64.b64encode(compressed).decode("ascii")


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

_last_backup_ts: datetime | None = None


def backup(force: bool = False, reason: str = "") -> dict:
    """
    Backup the DB to the configured Gist.

    Returns a status dict — never raises. Safe to call from anywhere.
    """
    global _last_backup_ts
    result = {"ok": False, "reason": "", "size_kb": 0,
              "gist_id": None, "skipped": False}

    if not is_configured():
        result["reason"] = "GITHUB_TOKEN not set"
        return result

    # Rate limit
    now = datetime.now(MYT)
    if not force and _last_backup_ts:
        elapsed = (now - _last_backup_ts).total_seconds()
        if elapsed < MIN_BACKUP_INTERVAL_SEC:
            result["skipped"] = True
            result["reason"] = f"rate-limited ({elapsed:.0f}s < "\
                               f"{MIN_BACKUP_INTERVAL_SEC}s)"
            return result

    with _BACKUP_LOCK:
        try:
            encoded = _encode_db_for_gist()
            size_kb = len(encoded) / 1024
            result["size_kb"] = round(size_kb, 1)

            marker = _read_marker()
            gist_id = marker.get("gist_id")

            files = {GIST_FILENAME: {"content": encoded}}

            # v3.1.6: also include the ML classifier .pkl if it exists
            ml_encoded = _encode_ml_for_gist()
            ml_size_kb = 0.0
            if ml_encoded:
                files[ML_GIST_FILENAME] = {"content": ml_encoded}
                ml_size_kb = len(ml_encoded) / 1024

            payload = {
                "description": (
                    f"BursaAI agent DB backup — "
                    f"{now.strftime('%Y-%m-%d %H:%M:%S')} MYT. "
                    f"Reason: {reason or 'periodic'}. "
                    f"DB: {size_kb:.1f} KB | ML: {ml_size_kb:.1f} KB "
                    f"(both compressed)."
                ),
                "files": files,
            }

            if gist_id:
                # Update existing gist
                r = requests.patch(f"{GIST_API}/{gist_id}",
                                   json=payload,
                                   headers=_headers(),
                                   timeout=30)
            else:
                # First backup — create new private gist
                payload["public"] = False
                r = requests.post(GIST_API, json=payload,
                                  headers=_headers(), timeout=30)

            if r.status_code in (200, 201):
                gist_id = r.json().get("id")
                _write_marker({
                    "gist_id": gist_id,
                    "last_backup_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_backup_size_kb": round(size_kb, 1),
                    "last_reason": reason,
                })
                _last_backup_ts = now
                result.update({"ok": True, "gist_id": gist_id,
                                "reason": reason or "ok"})
                log.info(f"backup OK ({size_kb:.1f} KB) → gist {gist_id}")
            else:
                result["reason"] = f"HTTP {r.status_code}: {r.text[:200]}"
                log.error(f"backup failed: {result['reason']}")
        except Exception as e:
            result["reason"] = f"exception: {e}"
            log.error(f"backup exception: {e}")

    return result


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore(gist_id: str | None = None) -> dict:
    """
    Restore the DB from the configured Gist. Called once on boot,
    BEFORE the scheduler starts.

    If `gist_id` not given, reads from DB meta (survives container resets)
    then local marker file, then GIST_ID env var.
    """
    result = {"ok": False, "reason": "", "bytes_restored": 0,
              "gist_id": None}

    if not is_configured():
        result["reason"] = "GITHUB_TOKEN not set"
        return result

    if gist_id is None:
        # v3.1.9: DB meta is primary (survives restore), then file, then env
        marker = _read_marker()
        gist_id = marker.get("gist_id")
        if not gist_id:
            gist_id = os.environ.get("GIST_ID")

    if not gist_id:
        result["reason"] = "no gist_id in meta, marker, or GIST_ID env (first run — nothing to restore)"
        return result

    try:
        r = requests.get(f"{GIST_API}/{gist_id}",
                         headers=_headers(), timeout=30)
        if r.status_code != 200:
            result["reason"] = f"HTTP {r.status_code}: {r.text[:200]}"
            return result

        gist = r.json()
        files = gist.get("files", {})
        if GIST_FILENAME not in files:
            result["reason"] = f"gist {gist_id} has no file '{GIST_FILENAME}'"
            return result

        def _fetch_file_content(file_meta):
            """Get file content, handling truncated gists via raw_url."""
            if file_meta.get("truncated") or not file_meta.get("content"):
                raw_url = file_meta.get("raw_url")
                if not raw_url:
                    return None
                r2 = requests.get(raw_url, headers=_headers(), timeout=60)
                return r2.text
            return file_meta["content"]

        encoded = _fetch_file_content(files[GIST_FILENAME])
        if encoded is None:
            result["reason"] = "DB file truncated with no raw_url"
            return result

        # SAFETY: backup the existing DB before overwriting (just in case)
        if os.path.exists(DB_PATH):
            backup_path = DB_PATH + ".pre_restore"
            try:
                import shutil
                shutil.copy2(DB_PATH, backup_path)
            except Exception:
                pass

        bytes_restored = _decode_gist_to_db(encoded.strip(), DB_PATH)

        # v3.1.13: re-apply schema migrations after restore.
        # Critical for forward-compat: the Gist backup may have been made
        # before a column was added (e.g. cycle_started_at in v3.1.10).
        # Without this, the restored DB has stale schema and the next
        # write to a new column crashes with sqlite3.OperationalError.
        # init_db() is idempotent — safe to call.
        try:
            from db import init_db as _init_db_after_restore
            _init_db_after_restore()
            log.info("post-restore init_db() ran — pending migrations applied")
        except Exception as _mig_err:
            # Don't fail the whole restore over a migration glitch —
            # the restore itself succeeded. Log and continue.
            log.error(f"post-restore init_db failed: {_mig_err}")

        # v3.1.6: also restore the ML classifier .pkl if present
        ml_bytes = 0
        if ML_GIST_FILENAME in files:
            try:
                ml_encoded = _fetch_file_content(files[ML_GIST_FILENAME])
                if ml_encoded:
                    ml_bytes = _decode_gist_to_db(ml_encoded.strip(),
                                                    ML_MODEL_PATH)
                    log.info(f"ML classifier restored ({ml_bytes} bytes)")
            except Exception as e:
                log.warning(f"ML classifier restore failed (non-fatal): {e}")

        # v3.1.9: store the gist_id in DB meta so next container reset
        # can find it even if the marker file is wiped.
        try:
            _write_marker({
                "gist_id": gist_id,
                "restored_at": datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            pass

        result.update({"ok": True, "bytes_restored": bytes_restored,
                        "ml_bytes_restored": ml_bytes,
                        "gist_id": gist_id,
                        "reason": (f"restored DB + ML"
                                   if ml_bytes else "restored DB only")})
        log.info(f"restore OK (DB={bytes_restored}, ML={ml_bytes}) "
                  f"← gist {gist_id}")
    except Exception as e:
        result["reason"] = f"exception: {e}"
        log.error(f"restore exception: {e}")

    return result


# ---------------------------------------------------------------------------
# Status (for dashboard)
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Returns the current backup status for the Settings tab UI."""
    marker = _read_marker()
    return {
        "configured": is_configured(),
        "gist_id": marker.get("gist_id") or os.environ.get("GIST_ID"),
        "last_backup_at": marker.get("last_backup_at"),
        "last_backup_size_kb": marker.get("last_backup_size_kb"),
        "last_reason": marker.get("last_reason"),
        "db_size_kb": (round(os.path.getsize(DB_PATH) / 1024, 1)
                       if os.path.exists(DB_PATH) else 0),
    }


# ---------------------------------------------------------------------------
# Boot-time restore (called from app.py BEFORE scheduler.ensure_started)
# ---------------------------------------------------------------------------

_BOOT_RESTORE_ATTEMPTED = False


def boot_restore_once() -> dict:
    """
    Idempotent boot-time restore. Called from app.py top-of-script.

    Only runs once per Python process. If the DB already exists and
    looks healthy (has the `account` row), skips restore — assumes
    the DB persisted from a previous boot in the same container.
    """
    global _BOOT_RESTORE_ATTEMPTED
    if _BOOT_RESTORE_ATTEMPTED:
        return {"skipped": True, "reason": "already attempted this process"}
    _BOOT_RESTORE_ATTEMPTED = True

    if not is_configured():
        return {"skipped": True, "reason": "GITHUB_TOKEN not set"}

    # Check if local DB already has data — if yes, don't overwrite
    try:
        from db import connect
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT cash_balance FROM account WHERE id=1"
            ).fetchone()
        if row and row["cash_balance"] is not None:
            # Local DB is populated — only restore if it's empty/fresh.
            # Check: is there at least 1 trade or 1 state_prior row?
            with connect(readonly=True) as c:
                t = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                p = c.execute("SELECT COUNT(*) FROM state_priors").fetchone()[0]
            if t > 0 or p > 0:
                log.info(
                    f"boot-restore skipped: local DB has data "
                    f"({t} trades, {p} state priors)"
                )
                return {"skipped": True,
                        "reason": f"local DB has data ({t} trades, {p} priors)"}
    except Exception as e:
        log.warning(f"boot-restore precheck failed (will attempt restore): {e}")

    return restore()
