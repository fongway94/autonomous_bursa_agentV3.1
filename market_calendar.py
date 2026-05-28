# market_calendar.py
"""
Bursa Malaysia market calendar — accurate trading sessions + public holidays.

Sessions
--------
    Monday–Friday (except public holidays)

    08:30 – 09:00   Pre-opening
    09:00 – 12:30   Morning session
    12:30 – 14:00   Lunch break (closed)
    14:00 – 14:30   Afternoon pre-open
    14:30 – 16:45   Afternoon session
    16:45 – 16:50   Pre-closing
    16:50 – 17:00   Trading at Last
"""

from __future__ import annotations
from datetime import datetime, time, timezone, timedelta
from typing import NamedTuple


MYT = timezone(timedelta(hours=8))


class Session(NamedTuple):
    name: str
    start: time
    end: time
    fills: bool


BURSA_SESSIONS = [
    Session("PRE_OPEN_AM",     time(8, 30),  time(9, 0),  False),
    Session("MORNING",         time(9, 0),   time(12, 30), True),
    Session("LUNCH_BREAK",     time(12, 30), time(14, 0), False),
    Session("PRE_OPEN_PM",     time(14, 0),  time(14, 30), False),
    Session("AFTERNOON",       time(14, 30), time(16, 45), True),
    Session("PRE_CLOSE",       time(16, 45), time(16, 50), True),
    Session("TRADING_AT_LAST", time(16, 50), time(17, 0), True),
]


def current_session(now: datetime | None = None) -> Session | None:
    if now is None:
        now = datetime.now(MYT)
    if not is_trading_day(now.date()):
        return None
    t = now.time()
    for s in BURSA_SESSIONS:
        if s.start <= t < s.end:
            return s
    return None


def is_market_open(now: datetime | None = None) -> bool:
    s = current_session(now)
    return s is not None and s.fills


def is_safe_entry_window(now: datetime | None = None) -> bool:
    if now is None:
        now = datetime.now(MYT)
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    morning_ok = time(9, 0) <= t < time(12, 30)
    afternoon_ok = time(14, 30) <= t < time(16, 0)
    return morning_ok or afternoon_ok


def next_session_start(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(MYT)
    today = now.date()
    if is_trading_day(today):
        for s in BURSA_SESSIONS:
            session_start = datetime.combine(today, s.start, tzinfo=MYT)
            if session_start > now:
                return session_start
    d = today
    for _ in range(10):
        d = d + timedelta(days=1)
        if is_trading_day(d):
            return datetime.combine(d, time(9, 0), tzinfo=MYT)
    return now + timedelta(hours=24)


MY_PUBLIC_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-29", "2025-01-30", "2025-02-11", "2025-03-18",
    "2025-03-31", "2025-04-01", "2025-05-01", "2025-05-12", "2025-06-02",
    "2025-06-07", "2025-06-27", "2025-08-31", "2025-09-05", "2025-09-16",
    "2025-10-20", "2025-12-25",
    # 2026
    "2026-01-01", "2026-02-17", "2026-02-18", "2026-03-02", "2026-03-21",
    "2026-03-22", "2026-03-23", "2026-05-01", "2026-05-27", "2026-06-06",
    "2026-06-16", "2026-08-25", "2026-08-31", "2026-09-16", "2026-11-08",
    "2026-12-25",
    # 2027 (verify before Jan 2027)
    "2027-01-01", "2027-02-06", "2027-02-07", "2027-05-01", "2027-08-31",
    "2027-09-16", "2027-12-25",
}


def is_public_holiday(d) -> bool:
    if hasattr(d, "strftime"):
        d = d.strftime("%Y-%m-%d")
    return d in MY_PUBLIC_HOLIDAYS


def is_trading_day(d) -> bool:
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    if d.weekday() >= 5:
        return False
    return not is_public_holiday(d)


def market_status_text(now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now(MYT)

    if not is_trading_day(now.date()):
        is_hol = is_public_holiday(now.date())
        reason = ("Malaysian public holiday — Bursa closed"
                  if is_hol else "Weekend — Bursa closed")
        nxt = next_session_start(now)
        return {
            "open": False,
            "session": "CLOSED_HOLIDAY" if is_hol else "CLOSED_WEEKEND",
            "reason": reason,
            "next_event": nxt.strftime("%Y-%m-%d %H:%M MYT"),
        }

    sess = current_session(now)
    if sess is None:
        nxt = next_session_start(now)
        return {
            "open": False,
            "session": "PRE_MARKET" if now.time() < time(8, 30) else "POST_CLOSE",
            "reason": "Outside Bursa sessions",
            "next_event": nxt.strftime("%Y-%m-%d %H:%M MYT"),
        }

    nxt = next_session_start(now)
    return {
        "open": sess.fills,
        "session": sess.name,
        "reason": (f"{sess.name} session "
                   f"({sess.start.strftime('%H:%M')}–"
                   f"{sess.end.strftime('%H:%M')})"),
        "next_event": nxt.strftime("%Y-%m-%d %H:%M MYT"),
    }
