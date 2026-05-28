# maintenance_reminders.py
"""
Long-term maintenance reminder system.

The agent is designed to run indefinitely, but three items need periodic
human attention:

  1. Public holiday list update — must append next year's MY holidays
     each January (Bursa publishes in late December).
  2. GitHub PAT regeneration — Personal Access Tokens expire (typically
     yearly). Without rotation, Gist backups silently fail.
  3. Walk-forward optimization re-run — market dynamics shift over time.
     Quarterly re-tuning keeps the scanner params calibrated.

This module computes the "due/overdue" state of each reminder and provides
banner data for the UI to render at the top of the dashboard.

Each reminder has three states:
  - "ok"      : nothing to do, next check is far in the future
  - "due"     : within 30 days of the action date — show yellow banner
  - "overdue" : past the action date — show red banner, blocks dismissal

Dismissal is per-reminder per-session (Streamlit session_state) so users
aren't nagged on every page click but still get re-reminded on each
fresh visit to the app.
"""

from __future__ import annotations
import os
from datetime import date, datetime, timezone, timedelta

MYT = timezone(timedelta(hours=8))


def _today_myt() -> date:
    return datetime.now(MYT).date()


# --------------------------------------------------------------------------
# Reminder 1: Public holidays
# --------------------------------------------------------------------------

def _check_holiday_list() -> dict:
    """
    Inspect market_calendar.MY_PUBLIC_HOLIDAYS to determine if the
    upcoming year's holidays have been appended.

    Logic:
      - In Jan-Mar: REQUIRE this year's holidays to be present
      - In Oct-Dec: REMIND to append next year's
      - Otherwise: no banner
    """
    try:
        from market_calendar import MY_PUBLIC_HOLIDAYS
    except Exception:
        return {"id": "holidays", "state": "ok"}

    today = _today_myt()
    this_year = today.year
    next_year = this_year + 1

    # Count holidays per year
    by_year = {}
    for d in MY_PUBLIC_HOLIDAYS:
        try:
            y = int(d.split("-")[0])
            by_year[y] = by_year.get(y, 0) + 1
        except Exception:
            pass

    this_year_count = by_year.get(this_year, 0)
    next_year_count = by_year.get(next_year, 0)

    # Late in the year → remind to add next year's
    if today.month >= 10 and next_year_count < 10:
        return {
            "id": "holidays",
            "state": "due",
            "title": f"📅 Append {next_year} Bursa public holidays",
            "message": (
                f"Only {next_year_count} holidays are in the calendar for "
                f"{next_year}. Bursa Malaysia typically publishes its full "
                f"trading calendar in November/December. Update "
                f"`market_calendar.MY_PUBLIC_HOLIDAYS` before {next_year}-01-01 "
                f"to avoid the agent scanning on holiday dates."
            ),
            "action": (
                "Edit `market_calendar.py` → add ~17 holiday dates for "
                f"{next_year} in `YYYY-MM-DD` format."
            ),
            "deadline": f"{next_year}-01-01",
        }

    # Early in the year → if this year is sparse, it's overdue
    if today.month <= 3 and this_year_count < 10:
        return {
            "id": "holidays",
            "state": "overdue",
            "title": f"⚠️ {this_year} Bursa public holidays missing",
            "message": (
                f"Only {this_year_count} holidays in the calendar for "
                f"{this_year}. The agent will scan on holiday dates and "
                f"get stale data. Fix immediately."
            ),
            "action": (
                "Edit `market_calendar.py` → add the full Bursa Malaysia "
                f"{this_year} holiday list."
            ),
            "deadline": f"{this_year}-01-01 (already passed!)",
        }

    return {"id": "holidays", "state": "ok"}


# --------------------------------------------------------------------------
# Reminder 2: GitHub PAT expiry
# --------------------------------------------------------------------------

def _check_github_token() -> dict:
    """
    We can't reliably tell when the PAT was created (no metadata).

    Strategy: track first-seen date in the gist marker, then remind 11
    months later (most users pick 1-year expiry). Falls back gracefully
    if persistence module isn't configured.
    """
    try:
        from persistence import is_configured, _read_marker, _write_marker
    except Exception:
        return {"id": "github_token", "state": "ok"}

    if not is_configured():
        # Token not set — separate concern, handled by persistence panel itself
        return {"id": "github_token", "state": "ok"}

    marker = _read_marker()
    first_seen = marker.get("token_first_seen_at")

    if not first_seen:
        # First time we've seen this token — record the date
        marker["token_first_seen_at"] = _today_myt().isoformat()
        try:
            _write_marker(marker)
        except Exception:
            pass
        return {"id": "github_token", "state": "ok"}

    try:
        first_date = date.fromisoformat(first_seen)
    except Exception:
        return {"id": "github_token", "state": "ok"}

    days_since = (_today_myt() - first_date).days
    days_until_warning = 330 - days_since   # 11 months
    days_until_overdue = 365 - days_since   # 12 months

    if days_until_overdue <= 0:
        return {
            "id": "github_token",
            "state": "overdue",
            "title": "🚨 GitHub PAT likely expired — backups may be failing",
            "message": (
                f"Your GitHub Personal Access Token has been in use for "
                f"{days_since} days. Most PATs expire at 365 days. If your "
                f"Settings tab shows backup failures, this is the cause."
            ),
            "action": (
                "1. Generate a new classic PAT at "
                "https://github.com/settings/tokens (scope: `gist`)\n"
                "2. Replace `GITHUB_TOKEN` in Streamlit Cloud Secrets\n"
                "3. Restart the app\n"
                "4. Verify backup works in ⚙️ Settings → 🗄️ Persistent Backup\n"
                "5. Reset the timer below"
            ),
            "deadline": "ASAP",
            "show_reset_button": True,
        }
    elif days_until_warning <= 0:
        return {
            "id": "github_token",
            "state": "due",
            "title": "📅 GitHub PAT will expire soon",
            "message": (
                f"Your GitHub Personal Access Token has been in use for "
                f"{days_since} days. If you set a 1-year expiry, you have "
                f"~{days_until_overdue} days left. Rotate it now to avoid "
                f"backup failures."
            ),
            "action": (
                "1. Generate a new classic PAT at "
                "https://github.com/settings/tokens (scope: `gist`)\n"
                "2. Replace `GITHUB_TOKEN` in Streamlit Cloud Secrets\n"
                "3. Restart the app\n"
                "4. Reset the timer below"
            ),
            "deadline": f"~{days_until_overdue} days",
            "show_reset_button": True,
        }

    return {
        "id": "github_token",
        "state": "ok",
        "days_until_warning": days_until_warning,
        "days_since": days_since,
    }


def reset_github_token_timer() -> None:
    """Called after user rotates their PAT — resets the first-seen timer."""
    try:
        from persistence import _read_marker, _write_marker
        marker = _read_marker()
        marker["token_first_seen_at"] = _today_myt().isoformat()
        _write_marker(marker)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Reminder 3: Walk-forward optimization re-run
# --------------------------------------------------------------------------

def _check_walk_forward() -> dict:
    """
    Reminds quarterly. Only relevant once the agent has 100+ closed trades
    (before that, not enough data for WFO to be meaningful).
    """
    try:
        from repository import closed_trades
        from db import connect
    except Exception:
        return {"id": "walk_forward", "state": "ok"}

    try:
        n_closed = len(closed_trades())
    except Exception:
        n_closed = 0

    if n_closed < 100:
        return {"id": "walk_forward", "state": "ok",
                "reason": f"only {n_closed} closed trades (need 100+)"}

    # Find last WALK_FORWARD_OPTIMIZATION event
    try:
        with connect(readonly=True) as c:
            row = c.execute(
                "SELECT timestamp FROM learning_events "
                "WHERE event_type='WALK_FORWARD_OPTIMIZATION' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception:
        return {"id": "walk_forward", "state": "ok"}

    if row is None:
        # Never run, but agent has 100+ trades — overdue
        return {
            "id": "walk_forward",
            "state": "due",
            "title": "📏 Run walk-forward optimization",
            "message": (
                f"You have {n_closed} closed trades but have never run "
                "walk-forward optimization. Quarterly re-tuning keeps the "
                "scanner parameters calibrated to current market regime."
            ),
            "action": (
                "Go to **🧠 AI Learning tab** → click "
                "**📏 Run Walk-Forward Optimization**. Takes ~2-5 minutes."
            ),
            "deadline": "any time",
        }

    try:
        last_ts = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
    except Exception:
        return {"id": "walk_forward", "state": "ok"}

    days_since = (_today_myt() - last_ts).days

    if days_since >= 90:
        return {
            "id": "walk_forward",
            "state": "due" if days_since < 180 else "overdue",
            "title": "📏 Quarterly walk-forward re-run due",
            "message": (
                f"Last walk-forward optimization ran {days_since} days ago. "
                "Market dynamics shift over time — re-running quarterly "
                "keeps the scanner params calibrated. "
                f"You now have {n_closed} closed trades to feed it."
            ),
            "action": (
                "Go to **🧠 AI Learning tab** → click "
                "**📏 Run Walk-Forward Optimization**."
            ),
            "deadline": f"{days_since} days since last run",
        }

    return {"id": "walk_forward", "state": "ok",
            "days_since_last": days_since}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get_all_reminders() -> list[dict]:
    """
    Returns ALL active reminders (due or overdue). Each is a dict with:
      - id, state ('due' | 'overdue'), title, message, action, deadline
      - optional: show_reset_button (for token reminder)
    Empty list if nothing needs attention.
    """
    out = []
    for fn in (_check_holiday_list, _check_github_token, _check_walk_forward):
        try:
            r = fn()
            if r and r.get("state") in ("due", "overdue"):
                out.append(r)
        except Exception:
            pass
    return out


def get_all_reminder_states() -> list[dict]:
    """
    Returns all reminders INCLUDING 'ok' ones, for the maintenance status
    panel in Settings tab. Each shows current status + next due date.
    """
    out = []
    for fn in (_check_holiday_list, _check_github_token, _check_walk_forward):
        try:
            r = fn()
            if r:
                out.append(r)
        except Exception:
            pass
    return out
