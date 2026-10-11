"""Phase 4: GoogleCalendarScheduler against a fake Google Calendar API (no network)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.tools import calendar as cal
from app.tools.calendar import GoogleCalendarScheduler, event_id_for

pytestmark = pytest.mark.anyio
PKT = ZoneInfo("Asia/Karachi")
MONDAY_8AM = datetime(2026, 10, 12, 8, 0, tzinfo=PKT)


class FakeHttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Req:
    """Mimics googleapiclient's request object: `.execute()` returns or raises."""

    def __init__(self, fn):
        self.execute = fn


class FakeCalendar:
    def __init__(self, busy=None, fail_first: int = 0, meet_pending: bool = False) -> None:
        self.busy = busy or []
        self.events_store: dict[str, dict] = {}
        self.inserts: list[dict] = []
        self.fail_first = fail_first
        self.meet_pending = meet_pending

    # service.freebusy().query(body=...).execute()
    def freebusy(self):
        return self

    def query(self, body):
        def run():
            if self.fail_first:
                self.fail_first -= 1
                raise FakeHttpError(503)
            return {"calendars": {"primary": {"busy": self.busy}}}

        return Req(run)

    # service.events().insert/get(...).execute()
    def events(self):
        return self

    def insert(self, calendarId, body, conferenceDataVersion, sendUpdates):
        def run():
            if body["id"] in self.events_store:
                raise FakeHttpError(409)
            self.inserts.append(
                {"body": body, "sendUpdates": sendUpdates, "cdv": conferenceDataVersion}
            )
            event = {**body, "status": "confirmed"}
            if not self.meet_pending:
                event["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
            self.events_store[body["id"]] = event
            return event

        return Req(run)

    def get(self, calendarId, eventId):
        def run():
            if eventId not in self.events_store:
                raise FakeHttpError(404)
            event = self.events_store[eventId]
            if self.meet_pending:  # link appears on the next read
                event["conferenceData"] = {
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "https://meet.google.com/xyz"}
                    ]
                }
            return event

        return Req(run)


@pytest.fixture(autouse=True)
def fixed_time_and_fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return MONDAY_8AM.astimezone(tz) if tz else MONDAY_8AM

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(cal, "datetime", FixedDateTime)
    monkeypatch.setattr(cal.asyncio, "sleep", no_sleep)


def scheduler(fake: FakeCalendar, **settings) -> GoogleCalendarScheduler:
    return GoogleCalendarScheduler(Settings(**settings), fake)


async def book(s: GoogleCalendarScheduler, key: str = "C001-abc", email: str | None = "c@x.com"):
    return await s.book_interview(
        candidate_name="Sara Khan",
        candidate_email=email,
        role="Python Intern",
        description="summary",
        booking_key=key,
    )


async def test_books_first_free_slot_with_meet_link() -> None:
    fake = FakeCalendar(
        busy=[{"start": "2026-10-12T10:00:00+05:00", "end": "2026-10-12T11:00:00+05:00"}]
    )
    booked = await book(scheduler(fake))
    assert booked.start == datetime(2026, 10, 12, 11, 0, tzinfo=PKT)  # 10-11 busy
    assert booked.meet_link == "https://meet.google.com/abc-defg-hij"
    ins = fake.inserts[0]
    assert ins["cdv"] == 1  # conferenceDataVersion=1 is required for Meet
    assert (
        ins["body"]["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"]
        == "hangoutsMeet"
    )
    assert ins["body"]["attendees"][0]["email"] == "c@x.com"
    assert ins["sendUpdates"] == "all"
    assert ins["body"]["start"]["timeZone"] == "Asia/Karachi"


async def test_same_booking_key_is_idempotent() -> None:
    fake = FakeCalendar()
    s = scheduler(fake)
    first = await book(s)
    second = await book(s)
    assert first == second
    assert len(fake.inserts) == 1
    assert first.calendar_event_id == event_id_for("C001-abc")


async def test_retries_transient_google_errors() -> None:
    fake = FakeCalendar(fail_first=2)
    assert (await book(scheduler(fake))) is not None


async def test_waits_for_pending_meet_link() -> None:
    booked = await book(scheduler(FakeCalendar(meet_pending=True)))
    assert booked.meet_link == "https://meet.google.com/xyz"


async def test_no_invite_without_email_or_when_disabled() -> None:
    fake = FakeCalendar()
    await book(scheduler(fake), key="k1", email=None)
    await book(scheduler(fake, calendar_invite_candidate=False), key="k2")
    assert all("attendees" not in i["body"] and i["sendUpdates"] == "none" for i in fake.inserts)


async def test_fully_busy_week_returns_none() -> None:
    fake = FakeCalendar(
        busy=[{"start": "2026-10-12T00:00:00+05:00", "end": "2026-10-31T00:00:00+05:00"}]
    )
    assert await book(scheduler(fake)) is None
    assert fake.inserts == []


def test_event_id_uses_only_allowed_characters() -> None:
    eid = event_id_for("C001-0ec8cff9")
    assert set(eid) <= set("abcdefghijklmnopqrstuv0123456789") and 5 <= len(eid) <= 1024
