"""Slot finder rules: Mon-Fri, 10:00-17:00 PKT, 30 min, lead time, skip busy times."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.tools.calendar import find_free_slot

PKT = ZoneInfo("Asia/Karachi")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PKT)  # Oct 2026: 12th = Monday


def slot(now: datetime, busy=None, **kw):
    return find_free_slot(now, busy or [], tz=PKT, **kw)


def test_morning_gets_slot_after_lead_time() -> None:
    assert slot(at(12, 9)) == (at(12, 11), at(12, 11, 30))  # 9:00 + 2h lead


def test_lead_time_rounds_up_to_next_half_hour() -> None:
    assert slot(at(12, 10, 10))[0] == at(12, 12, 30)


def test_after_hours_rolls_to_next_day() -> None:
    assert slot(at(12, 16))[0] == at(13, 10)


def test_last_slot_must_end_by_five() -> None:
    assert slot(at(12, 14, 30))[0] == at(12, 16, 30)  # 16:30-17:00 is the last slot


def test_friday_evening_rolls_to_monday() -> None:
    assert slot(at(16, 18))[0] == at(19, 10)  # Fri 16 Oct -> Mon 19 Oct


def test_skips_busy_intervals() -> None:
    busy = [(at(13, 10), at(13, 11, 15))]
    assert slot(at(12, 18), busy)[0] == at(13, 11, 30)


def test_returns_none_when_fully_booked() -> None:
    busy = [(at(12, 0), at(31, 0))]
    assert slot(at(12, 8), busy) is None


def test_converts_other_timezones_to_pkt() -> None:
    utc_now = datetime(2026, 10, 12, 4, 0, tzinfo=ZoneInfo("UTC"))  # = 09:00 PKT
    assert slot(utc_now)[0] == at(12, 11)


def test_custom_slot_length() -> None:
    start, end = slot(at(12, 8), slot_minutes=45, min_lead=timedelta(0))
    assert (start, end) == (at(12, 10), at(12, 10, 45))
