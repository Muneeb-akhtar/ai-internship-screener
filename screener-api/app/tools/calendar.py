"""Interview scheduling.

Phase 2 ships:
  - `find_free_slot()`: a pure function (no network) that finds the next free
    30-minute slot on working days 10:00-17:00 in HR's timezone, skipping busy times.
  - `LocalScheduler`: uses it with NO busy times and NO Meet link, so the agent
    works end to end locally.
Phase 4 adds `GoogleCalendarScheduler` (free/busy lookup + event with a Meet link)
behind the same `InterviewScheduler` interface - the agent code won't change.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.config import Settings


@dataclass(frozen=True)
class BookedInterview:
    start: datetime  # timezone-aware
    end: datetime
    meet_link: str | None
    calendar_event_id: str | None


class InterviewScheduler(Protocol):
    async def book_interview(
        self, *, candidate_name: str, candidate_email: str | None, role: str, description: str
    ) -> BookedInterview | None:
        """Reserve the next free slot. Returns None if no slot is available."""
        ...


def find_free_slot(
    now: datetime,
    busy: list[tuple[datetime, datetime]],
    *,
    tz: ZoneInfo,
    day_start_hour: int = 10,
    day_end_hour: int = 17,
    slot_minutes: int = 30,
    lookahead_working_days: int = 5,
    min_lead: timedelta = timedelta(hours=2),
) -> tuple[datetime, datetime] | None:
    """Return (start, end) of the earliest free slot, or None.

    Rules: Monday-Friday only; slots start on the hour or half hour between
    day_start_hour and day_end_hour (the slot must END by day_end_hour); a slot must
    start at least `min_lead` after `now`; it must not overlap any busy interval.
    "Next N working days" counts today if it is a working day.
    """
    now = now.astimezone(tz)
    earliest = now + min_lead
    step = timedelta(minutes=slot_minutes)
    day = now.date()
    working_days_seen = 0

    while working_days_seen < lookahead_working_days:
        if day.weekday() < 5:  # 0=Mon ... 4=Fri
            working_days_seen += 1
            start = datetime(day.year, day.month, day.day, day_start_hour, tzinfo=tz)
            day_end = datetime(day.year, day.month, day.day, day_end_hour, tzinfo=tz)
            while start + step <= day_end:
                end = start + step
                # Two intervals overlap when each starts before the other ends
                overlaps = any(b_start < end and start < b_end for b_start, b_end in busy)
                if start >= earliest and not overlaps:
                    return start, end
                start += step
        day += timedelta(days=1)
    return None


class LocalScheduler:
    """Phase 2 stand-in: picks the next slot assuming HR's calendar is empty. No Meet link."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._tz = ZoneInfo(settings.hr_timezone)

    async def book_interview(
        self, *, candidate_name: str, candidate_email: str | None, role: str, description: str
    ) -> BookedInterview | None:
        slot = find_free_slot(
            datetime.now(self._tz),
            busy=[],
            tz=self._tz,
            day_start_hour=self._s.work_day_start_hour,
            day_end_hour=self._s.work_day_end_hour,
            slot_minutes=self._s.interview_slot_minutes,
            lookahead_working_days=self._s.scheduling_lookahead_working_days,
            min_lead=timedelta(hours=self._s.scheduling_min_lead_hours),
        )
        if slot is None:
            return None
        return BookedInterview(start=slot[0], end=slot[1], meet_link=None, calendar_event_id=None)
