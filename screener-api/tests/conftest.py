"""Shared pytest fixtures. Sets safe fake settings so tests never need a real .env,
and provides a FakeLLM + FakeScheduler so the agent runs with no network and no cost."""

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

TEST_API_KEY = "test-api-key-0123456789"

# Must be set before app.config.get_settings() is first called
os.environ.update(
    {
        "APP_ENV": "test",
        "SCREENER_API_KEY": TEST_API_KEY,
        "SCREENER_DB_PASSWORD": "not-used-in-tests",
    }
)

from app.agent.state import CandidateAnswers, Scores  # noqa: E402
from app.tools.calendar import BookedInterview  # noqa: E402

TRANSCRIPTS = Path(__file__).parent / "fixtures" / "transcripts"
PKT = ZoneInfo("Asia/Karachi")


def load_transcript(name: str) -> str:
    return (TRANSCRIPTS / f"{name}.txt").read_text(encoding="utf-8")


def make_scores(
    c: int, t: int, p: int, m: int, *, available: bool = True, confidence: float = 0.9
) -> Scores:
    return Scores(
        communication=c,
        technical=t,
        problem_solving=p,
        motivation=m,
        available=available,
        confidence=confidence,
        reasoning="test reasoning",
        summary="Test summary of the candidate.",
    )


def make_answers(*, consent: bool = True, asked_for_human: bool = False) -> CandidateAnswers:
    return CandidateAnswers(
        consent_given=consent,
        communication="intro",
        technical="answer",
        problem_solving="answer",
        motivation="answer",
        availability="full-time",
        asked_for_human=asked_for_human,
    )


class FakeLLM:
    """Stands in for the real LLM. Returns scripted answers and records every call.
    `scores` is a queue: each scoring attempt pops the next one (to test re-scoring)."""

    model_name = "fake-llm"

    def __init__(
        self,
        answers: CandidateAnswers | None = None,
        scores: list[Scores] | None = None,
        rejection: str | Exception = "Hi, thanks for applying. - The Hiring Team",
    ) -> None:
        self.answers = answers or make_answers()
        self.scores = list(scores or [])
        self.rejection = rejection
        self.calls: list[str] = []

    async def structured(self, schema: type[BaseModel], messages: list) -> BaseModel:
        self.calls.append(schema.__name__)
        if schema is CandidateAnswers:
            return self.answers
        if schema is Scores:
            return self.scores.pop(0)
        raise AssertionError(f"unexpected schema {schema}")

    async def text(self, messages: list) -> str:
        self.calls.append("text")
        if isinstance(self.rejection, Exception):
            raise self.rejection
        return self.rejection


class FakeScheduler:
    """Always books Monday 12 Oct 2026, 10:00-10:30 PKT with a fake Meet link."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.bookings: list[dict] = []

    async def book_interview(self, **kwargs) -> BookedInterview | None:
        self.bookings.append(kwargs)
        if not self.available:
            return None
        start = datetime(2026, 10, 12, 10, 0, tzinfo=PKT)
        return BookedInterview(
            start=start,
            end=start + timedelta(minutes=30),
            meet_link="https://meet.google.com/abc-defg-hij",
            calendar_event_id="evt123",
        )


@pytest.fixture
def anyio_backend() -> str:
    # Lets `@pytest.mark.anyio` tests run on asyncio (anyio ships with FastAPI)
    return "asyncio"


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)
