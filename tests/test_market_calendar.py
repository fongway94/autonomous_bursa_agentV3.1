"""
Tests for Bursa Malaysia market calendar accuracy.

Each session boundary must behave exactly as Bursa documents:
  09:00-12:30 morning (fills)
  12:30-14:00 lunch break (no fills)
  14:30-17:00 afternoon (fills)
  Plus pre-open/pre-close edge zones.

Safe-entry window is tighter than market-open:
  09:00-12:30 and 14:30-16:00 only.

Weekends and public holidays are always closed.
"""

from datetime import datetime, timezone, timedelta

MYT = timezone(timedelta(hours=8))


def _t(yyyymmdd_hhmm: str) -> datetime:
    """Helper: '2026-06-15 09:00' → tz-aware MYT datetime."""
    return datetime.strptime(yyyymmdd_hhmm, "%Y-%m-%d %H:%M").replace(tzinfo=MYT)


# ---- Trading day basics ----

def test_weekday_is_trading_day():
    from market_calendar import is_trading_day
    # Monday June 15, 2026 — regular weekday, not in holiday list
    assert is_trading_day(_t("2026-06-15 10:00").date()) is True


def test_saturday_is_not_trading_day():
    from market_calendar import is_trading_day
    # Saturday May 30, 2026
    assert is_trading_day(_t("2026-05-30 10:00").date()) is False


def test_sunday_is_not_trading_day():
    from market_calendar import is_trading_day
    assert is_trading_day(_t("2026-05-31 10:00").date()) is False


def test_public_holiday_is_not_trading_day():
    from market_calendar import is_trading_day, is_public_holiday
    # National Day 2026 — Aug 31 (Monday)
    assert is_public_holiday("2026-08-31") is True
    assert is_trading_day("2026-08-31") is False


def test_christmas_is_not_trading_day():
    from market_calendar import is_trading_day
    assert is_trading_day("2026-12-25") is False


# ---- Session timing (matching engine) ----

def test_pre_market_is_closed():
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 08:00")) is False
    assert is_market_open(_t("2026-06-15 08:59")) is False


def test_pre_open_phase_is_not_fillable():
    """08:30-09:00 is pre-open — orders accepted but no fills."""
    from market_calendar import is_market_open, current_session
    assert is_market_open(_t("2026-06-15 08:45")) is False
    s = current_session(_t("2026-06-15 08:45"))
    assert s is not None and s.name == "PRE_OPEN_AM"


def test_morning_open_at_9am():
    """The original bug: system thought open was 09:15."""
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 09:00")) is True
    assert is_market_open(_t("2026-06-15 09:01")) is True
    assert is_market_open(_t("2026-06-15 09:15")) is True


def test_morning_close_at_1230():
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 12:29")) is True
    assert is_market_open(_t("2026-06-15 12:30")) is False
    assert is_market_open(_t("2026-06-15 12:45")) is False


def test_lunch_break_is_closed():
    """The fix: scheduler used to scan during lunch, wasting yfinance calls."""
    from market_calendar import is_market_open, current_session
    assert is_market_open(_t("2026-06-15 13:00")) is False
    assert is_market_open(_t("2026-06-15 13:30")) is False
    s = current_session(_t("2026-06-15 13:00"))
    assert s is not None and s.name == "LUNCH_BREAK"


def test_afternoon_pre_open_not_fillable():
    """14:00-14:30 — pre-open, no fills."""
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 14:00")) is False
    assert is_market_open(_t("2026-06-15 14:29")) is False


def test_afternoon_open_at_1430():
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 14:30")) is True
    assert is_market_open(_t("2026-06-15 15:00")) is True
    assert is_market_open(_t("2026-06-15 16:00")) is True


def test_pre_close_and_tal_still_fillable():
    """16:45-17:00 — pre-close + Trading at Last, fills still happen."""
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 16:50")) is True
    assert is_market_open(_t("2026-06-15 16:59")) is True


def test_after_close():
    from market_calendar import is_market_open
    assert is_market_open(_t("2026-06-15 17:00")) is False
    assert is_market_open(_t("2026-06-15 18:00")) is False
    assert is_market_open(_t("2026-06-15 21:00")) is False


# ---- Safe-entry window (stricter, for new auto-entries) ----

def test_safe_entry_morning():
    from market_calendar import is_safe_entry_window
    assert is_safe_entry_window(_t("2026-06-15 09:00")) is True
    assert is_safe_entry_window(_t("2026-06-15 11:00")) is True
    assert is_safe_entry_window(_t("2026-06-15 12:29")) is True
    assert is_safe_entry_window(_t("2026-06-15 12:30")) is False


def test_safe_entry_blocks_lunch():
    from market_calendar import is_safe_entry_window
    assert is_safe_entry_window(_t("2026-06-15 13:00")) is False


def test_safe_entry_afternoon():
    from market_calendar import is_safe_entry_window
    assert is_safe_entry_window(_t("2026-06-15 14:30")) is True
    assert is_safe_entry_window(_t("2026-06-15 15:30")) is True
    assert is_safe_entry_window(_t("2026-06-15 15:59")) is True


def test_safe_entry_blocks_last_hour():
    """16:00 cutoff prevents entries that have <1h to develop."""
    from market_calendar import is_safe_entry_window, is_market_open
    # 16:00 — market still open but entries blocked
    assert is_market_open(_t("2026-06-15 16:00")) is True
    assert is_safe_entry_window(_t("2026-06-15 16:00")) is False
    assert is_safe_entry_window(_t("2026-06-15 16:30")) is False


def test_safe_entry_blocks_weekend():
    from market_calendar import is_safe_entry_window
    assert is_safe_entry_window(_t("2026-05-30 10:00")) is False


def test_safe_entry_blocks_holiday():
    from market_calendar import is_safe_entry_window
    assert is_safe_entry_window(_t("2026-08-31 10:00")) is False


# ---- Next-session computation ----

def test_next_session_from_pre_market():
    from market_calendar import next_session_start
    nxt = next_session_start(_t("2026-06-15 08:00"))
    assert nxt.hour == 8 and nxt.minute == 30  # PRE_OPEN_AM at 08:30


def test_next_session_from_lunch_break():
    from market_calendar import next_session_start
    nxt = next_session_start(_t("2026-06-15 13:00"))
    assert nxt.hour == 14 and nxt.minute == 0   # PRE_OPEN_PM at 14:00


def test_next_session_from_after_close():
    from market_calendar import next_session_start
    nxt = next_session_start(_t("2026-06-15 17:30"))   # Monday post-close
    assert nxt.date().isoformat() == "2026-06-17"      # Jun 16 is Awal Muharram
    assert nxt.hour == 9 and nxt.minute == 0


def test_next_session_skips_weekend():
    from market_calendar import next_session_start
    nxt = next_session_start(_t("2026-05-29 17:30"))   # Friday after close
    # Saturday May 30, Sunday May 31 → Monday June 1
    assert nxt.date().isoformat() == "2026-06-01"
    assert nxt.hour == 9


def test_next_session_skips_holiday():
    """After Sunday Aug 30 → Monday Aug 31 is National Day → skip to Sep 1."""
    from market_calendar import next_session_start
    nxt = next_session_start(_t("2026-08-30 17:30"))
    assert nxt.date().isoformat() == "2026-09-01"


# ---- Backwards compat with risk_manager ----

def test_risk_manager_check_trading_time_window_uses_calendar():
    """check_trading_time_window must now reject lunch break."""
    from unittest.mock import patch
    from risk_manager import check_trading_time_window

    # Force "now" = Monday 13:00 (in lunch break)
    fake_now = _t("2026-06-15 13:00")
    with patch("risk_manager.get_myt_now", return_value=fake_now), \
         patch("market_calendar.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.combine = datetime.combine
        mock_dt.strptime = datetime.strptime
        from market_calendar import is_market_open
        assert not is_market_open(fake_now)
        result = check_trading_time_window()
        assert result["allowed"] is False
        assert "LUNCH" in result["window"] or "closed" in result["reason"].lower()
