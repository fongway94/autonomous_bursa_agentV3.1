"""
Tests for v3.1.7 long-term maintenance reminder system.

Three reminders need to fire at the right times:
  1. Public holidays — if next year not in calendar by Oct
  2. GitHub PAT — 11-month warning, 12-month overdue
  3. Walk-forward optimization — quarterly after 100+ trades
"""

from datetime import date, timedelta


# ----- Public reminder API -----

def test_get_all_reminders_returns_list():
    from maintenance_reminders import get_all_reminders
    out = get_all_reminders()
    assert isinstance(out, list)


def test_get_all_reminder_states_returns_all_three():
    """Even when nothing is overdue, status panel shows all 3 reminders."""
    from maintenance_reminders import get_all_reminder_states
    states = get_all_reminder_states()
    ids = {s["id"] for s in states}
    assert "holidays" in ids
    assert "github_token" in ids
    assert "walk_forward" in ids


# ----- Holiday reminder logic -----

def test_holiday_check_with_full_calendar_is_ok(monkeypatch):
    """If next year already has plenty of holidays, no reminder."""
    import maintenance_reminders, market_calendar
    # Force "today" to be October 15 of the current year
    today = date.today()
    monkeypatch.setattr(maintenance_reminders, "_today_myt",
                        lambda: date(today.year, 10, 15))
    # Inject lots of next-year holidays
    next_year = today.year + 1
    fake_holidays = market_calendar.MY_PUBLIC_HOLIDAYS | {
        f"{next_year}-{m:02d}-01" for m in range(1, 13)
    }
    monkeypatch.setattr(market_calendar, "MY_PUBLIC_HOLIDAYS", fake_holidays)
    r = maintenance_reminders._check_holiday_list()
    assert r["state"] == "ok"


def test_holiday_check_late_year_with_empty_next_year_is_due(monkeypatch):
    """In October with no next-year holidays, reminder fires."""
    import maintenance_reminders, market_calendar
    today = date.today()
    monkeypatch.setattr(maintenance_reminders, "_today_myt",
                        lambda: date(today.year, 11, 15))
    # Strip ALL next-year holidays from the set
    next_year = today.year + 1
    fake = {h for h in market_calendar.MY_PUBLIC_HOLIDAYS
            if not h.startswith(str(next_year))}
    monkeypatch.setattr(market_calendar, "MY_PUBLIC_HOLIDAYS", fake)
    r = maintenance_reminders._check_holiday_list()
    assert r["state"] == "due"
    assert str(next_year) in r["title"]


def test_holiday_check_january_with_empty_current_year_is_overdue(monkeypatch):
    """In January with no current-year holidays, OVERDUE."""
    import maintenance_reminders, market_calendar
    today = date.today()
    monkeypatch.setattr(maintenance_reminders, "_today_myt",
                        lambda: date(today.year, 2, 15))
    # Strip all current-year holidays
    fake = {h for h in market_calendar.MY_PUBLIC_HOLIDAYS
            if not h.startswith(str(today.year))}
    monkeypatch.setattr(market_calendar, "MY_PUBLIC_HOLIDAYS", fake)
    r = maintenance_reminders._check_holiday_list()
    assert r["state"] == "overdue"


# ----- GitHub PAT reminder logic -----

def test_github_token_no_token_no_reminder(monkeypatch):
    """If GITHUB_TOKEN is not set, this reminder doesn't fire — it's a
    separate concern handled by the Persistent Backup panel itself."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    import maintenance_reminders
    r = maintenance_reminders._check_github_token()
    assert r["state"] == "ok"


def test_github_token_first_seen_records_today(monkeypatch, tmp_path):
    """First time we see a token, record today as the first_seen date."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    import persistence, maintenance_reminders
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))
    r = maintenance_reminders._check_github_token()
    assert r["state"] == "ok"
    # Marker should now have token_first_seen_at
    m = persistence._read_marker()
    assert "token_first_seen_at" in m


def test_github_token_11_months_old_is_due(monkeypatch, tmp_path):
    """11 months after first seen → warning."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    import persistence, maintenance_reminders
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))
    # Pre-seed marker with date 335 days ago
    old_date = (date.today() - timedelta(days=335)).isoformat()
    persistence._write_marker({"token_first_seen_at": old_date,
                                "gist_id": "fake"})
    r = maintenance_reminders._check_github_token()
    assert r["state"] == "due"
    assert "expire soon" in r["title"].lower()


def test_github_token_overdue_after_year(monkeypatch, tmp_path):
    """13 months after first seen → overdue, blocks dismissal."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    import persistence, maintenance_reminders
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))
    old_date = (date.today() - timedelta(days=400)).isoformat()
    persistence._write_marker({"token_first_seen_at": old_date,
                                "gist_id": "fake"})
    r = maintenance_reminders._check_github_token()
    assert r["state"] == "overdue"
    assert r.get("show_reset_button") is True


def test_reset_github_token_timer(monkeypatch, tmp_path):
    """After user rotates PAT, reset_github_token_timer resets the counter."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake_token")
    import persistence, maintenance_reminders
    monkeypatch.setattr(persistence, "MARKER_FILE",
                        str(tmp_path / "marker.json"))
    # Set old date → would be overdue
    old_date = (date.today() - timedelta(days=400)).isoformat()
    persistence._write_marker({"token_first_seen_at": old_date,
                                "gist_id": "fake"})
    assert maintenance_reminders._check_github_token()["state"] == "overdue"

    # Reset
    maintenance_reminders.reset_github_token_timer()
    # Now ok again
    assert maintenance_reminders._check_github_token()["state"] == "ok"


# ----- Walk-forward reminder logic -----

def test_walk_forward_below_100_trades_no_reminder():
    """Don't nag about WFO before user has 100 closed trades."""
    import maintenance_reminders
    # Fresh DB has 0 trades — no reminder
    r = maintenance_reminders._check_walk_forward()
    assert r["state"] == "ok"


def test_walk_forward_with_100_trades_never_run_is_due():
    """100+ trades but never optimized → due."""
    import maintenance_reminders
    from repository import insert_trade
    # Insert 100 fake closed trades
    for i in range(100):
        insert_trade({
            "ticker": f"T{i}.KL", "name": "x", "sector": "Tech",
            "signal_type": "GOLD BUY (BREAKOUT)",
            "entry_price": 1.0, "stop_loss": 0.9,
            "tp1": 1.1, "tp2": 1.2, "tp3": 1.3,
            "shares": 100, "lots": 1, "cost": 100, "fee": 0.15,
            "total_outlay": 100.15, "risk_per_share": 0.1,
            "actual_risk_pct": 10, "status": "CLOSED", "phase": "CLOSED",
            "outcome": "WIN",
            "logged_at": "2026-01-01 09:00:00",
            "closed_at": "2026-01-01 11:00:00",
            "shares_remaining": 0, "realized_pnl": 10,
        })
    r = maintenance_reminders._check_walk_forward()
    assert r["state"] == "due"
    assert "walk-forward" in r["title"].lower()
