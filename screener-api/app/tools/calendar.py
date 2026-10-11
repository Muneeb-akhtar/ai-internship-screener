"""Interview scheduling tools.

- `find_free_slot()`         pure function: next free 30-min slot on working days 10:00-17:00
                             in HR's timezone, skipping busy intervals (easy to unit-test).
- `LocalScheduler`           no Google: assumes an empty calendar, no Meet link (dev fallback).
- `GoogleCalendarScheduler`  real thing: Calendar free/busy -> find_free_slot -> insert event
                             with a Google Meet link.

All implement `InterviewScheduler`, so the agent code doesn't care which one it gets.

Google auth: OAuth for HR's own Google account (one-time consent via scripts/google_auth.py,
which saves a refresh token file). A service account can't create Meet links on a personal
@gmail.com calendar, which is why we use OAuth.
"""

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog

from app.config import Settings

log = structlog.get_logger()

# Least privilege: create/edit events + read free/busy. No access to event contents.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]


@dataclass(frozen=True)
class BookedInterview:
    start: datetime  # timezone-aware
    end: datetime
    meet_link: str | None
    calendar_event_id: str | None


class InterviewScheduler(Protocol):
    async def book_interview(
        self,
        *,
        candidate_name: str,
        candidate_email: str | None,
        role: str,
        description: str,
        booking_key: str,
    ) -> BookedInterview | None:
        """Reserve the next free slot. Returns None if no slot is available.
        `booking_key` (the LangGraph thread id) makes booking idempotent: calling twice
        with the same key returns the same event instead of creating a second one."""
        ...


# ----------------------------------------------------------------------------- slots
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


def search_window_end(now: datetime, working_days: int) -> datetime:
    """End of the last working day we'd consider (for the free/busy query)."""
    day, seen = now.date(), 0
    while True:
        if day.weekday() < 5:
            seen += 1
            if seen == working_days:
                return datetime(day.year, day.month, day.day, 23, 59, tzinfo=now.tzinfo)
        day += timedelta(days=1)


def _slot_kwargs(s: Settings) -> dict[str, Any]:
    return {
        "day_start_hour": s.work_day_start_hour,
        "day_end_hour": s.work_day_end_hour,
        "slot_minutes": s.interview_slot_minutes,
        "lookahead_working_days": s.scheduling_lookahead_working_days,
        "min_lead": timedelta(hours=s.scheduling_min_lead_hours),
    }


# ----------------------------------------------------------------------------- local
class LocalScheduler:
    """Dev fallback: picks the next slot assuming HR's calendar is empty. No Meet link."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._tz = ZoneInfo(settings.hr_timezone)

    async def book_interview(self, **_: Any) -> BookedInterview | None:
        slot = find_free_slot(datetime.now(self._tz), busy=[], tz=self._tz, **_slot_kwargs(self._s))
        if slot is None:
            return None
        return BookedInterview(start=slot[0], end=slot[1], meet_link=None, calendar_event_id=None)


# ----------------------------------------------------------------------------- google
def event_id_for(booking_key: str) -> str:
    """Deterministic Calendar event id (allowed chars: a-v and 0-9; hex digits qualify).
    Same thread -> same id -> Google rejects a duplicate insert with HTTP 409."""
    return "scr" + hashlib.sha256(booking_key.encode()).hexdigest()[:40]


def _meet_link(event: dict) -> str | None:
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for entry in (event.get("conferenceData") or {}).get("entryPoints", []):
        if entry.get("entryPointType") == "video":
            return entry.get("uri")
    return None


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)


class GoogleCalendarScheduler:
    """Books interviews in HR's Google Calendar with a Google Meet link.

    The Google client library is synchronous, so each call runs in a worker thread
    (asyncio.to_thread) to avoid blocking the event loop. A lock serialises
    "check free/busy -> insert" so two shortlists at the same moment can't grab the same slot.
    """

    def __init__(self, settings: Settings, service: Any) -> None:
        self._s = settings
        self._tz = ZoneInfo(settings.hr_timezone)
        self._service = service  # googleapiclient Resource (or a fake in tests)
        self._lock = asyncio.Lock()

    @classmethod
    def from_token_file(cls, settings: Settings) -> "GoogleCalendarScheduler":
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(settings.google_token_file, GOOGLE_SCOPES)
        # cache_discovery=False: no file cache warnings inside the container
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return cls(settings, service)

    async def _call(self, request: Any, attempts: int = 4) -> dict:
        """Execute a Google API request with retries on rate limits / server errors."""
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(request.execute)
            except Exception as exc:
                status = _status(exc)
                if status in (403, 429, 500, 502, 503, 504) and attempt < attempts:
                    await asyncio.sleep(2**attempt)  # 2, 4, 8 s
                    log.warning("google_retry", status=status, attempt=attempt)
                    continue
                raise
        raise RuntimeError("unreachable")

    async def busy_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self._s.hr_timezone,
            "items": [{"id": self._s.google_calendar_id}],
        }
        result = await self._call(self._service.freebusy().query(body=body))
        cal = result["calendars"][self._s.google_calendar_id]
        if cal.get("errors"):
            raise RuntimeError(f"free/busy error: {cal['errors']}")
        return [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in cal.get("busy", [])
        ]

    async def book_interview(
        self,
        *,
        candidate_name: str,
        candidate_email: str | None,
        role: str,
        description: str,
        booking_key: str,
    ) -> BookedInterview | None:
        events = self._service.events()
        cal_id = self._s.google_calendar_id
        event_id = event_id_for(booking_key)

        async with self._lock:
            # 1) Already booked for this evaluation? (retry / crash recovery)
            existing = await self._get_event(event_id)
            if existing and existing.get("status") != "cancelled":
                log.info("calendar_event_exists", event_id=event_id)
                return await self._to_booking(existing)

            # 2) Find the next free slot using HR's real busy times
            now = datetime.now(self._tz)
            busy = await self.busy_intervals(
                now, search_window_end(now, self._s.scheduling_lookahead_working_days)
            )
            slot = find_free_slot(now, busy, tz=self._tz, **_slot_kwargs(self._s))
            if slot is None:
                log.warning("calendar_no_free_slot", busy_count=len(busy))
                return None

            # 3) Create the event with a Meet link
            body: dict[str, Any] = {
                "id": event_id,
                "summary": f"Interview: {candidate_name} ({role})",
                "description": description,
                "start": {"dateTime": slot[0].isoformat(), "timeZone": self._s.hr_timezone},
                "end": {"dateTime": slot[1].isoformat(), "timeZone": self._s.hr_timezone},
                "conferenceData": {
                    "createRequest": {
                        "requestId": uuid.uuid4().hex,
                        "conferenceSolutionKey": {"type": "hangoutsMeet"},
                    }
                },
                "reminders": {"useDefault": True},
            }
            if candidate_email and self._s.calendar_invite_candidate:
                body["attendees"] = [{"email": candidate_email, "displayName": candidate_name}]
            try:
                event = await self._call(
                    events.insert(
                        calendarId=cal_id,
                        body=body,
                        conferenceDataVersion=1,  # required for Meet link creation
                        sendUpdates="all" if body.get("attendees") else "none",
                    )
                )
            except Exception as exc:
                if _status(exc) == 409:  # inserted by an earlier attempt that then failed
                    event = await self._get_event(event_id)
                    if event:
                        return await self._to_booking(event)
                raise
            log.info("calendar_event_created", event_id=event_id, start=slot[0].isoformat())
            return await self._to_booking(event)

    async def _get_event(self, event_id: str) -> dict | None:
        try:
            return await self._call(
                self._service.events().get(calendarId=self._s.google_calendar_id, eventId=event_id)
            )
        except Exception as exc:
            if _status(exc) in (404, 410):
                return None
            raise

    async def _to_booking(self, event: dict) -> BookedInterview:
        # Meet creation is usually instant but can be "pending" for a moment
        for _ in range(3):
            if _meet_link(event):
                break
            await asyncio.sleep(1.5)
            event = await self._get_event(event["id"]) or event
        tz = self._tz
        return BookedInterview(
            start=datetime.fromisoformat(event["start"]["dateTime"]).astimezone(tz),
            end=datetime.fromisoformat(event["end"]["dateTime"]).astimezone(tz),
            meet_link=_meet_link(event),
            calendar_event_id=event["id"],
        )


def build_scheduler(settings: Settings) -> InterviewScheduler:
    """Google Calendar if a token file exists, otherwise the local fallback."""
    from pathlib import Path

    if Path(settings.google_token_file).is_file():
        log.info("scheduler", kind="google_calendar", calendar=settings.google_calendar_id)
        return GoogleCalendarScheduler.from_token_file(settings)
    log.warning("scheduler", kind="local", reason=f"no token at {settings.google_token_file}")
    return LocalScheduler(settings)
