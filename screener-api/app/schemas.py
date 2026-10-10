"""Request/response models for the HTTP API (what n8n sends and receives)."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EvaluateRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    candidate_name: str = Field(min_length=1, max_length=200)
    candidate_email: str | None = Field(None, max_length=320)
    role: str = Field(min_length=1, max_length=200)
    transcript: str = Field(min_length=1, max_length=200_000)
    # Vapi's call id. Re-sending the same call_id returns the existing result
    # instead of evaluating twice (idempotency - webhooks are often retried).
    call_id: str | None = Field(None, max_length=200)
    call_duration_seconds: float | None = Field(None, ge=0)
    # From Vapi call metadata. When present, the API checks it belongs to candidate_id
    # (browser-supplied metadata is never trusted on its own).
    interview_token: str | None = Field(None, max_length=64)


class ReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reviewer: str | None = Field(None, max_length=200)
    note: str | None = Field(None, max_length=2000)

    @field_validator("decision", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v  # accept "approve"


class ScoreBreakdown(BaseModel):
    communication: int
    technical: int
    problem_solving: int
    motivation: int
    available: bool
    confidence: float
    total: int
    reasoning: str
    summary: str


class InterviewOut(BaseModel):
    start: str
    end: str
    timezone: str
    meet_link: str | None
    calendar_event_id: str | None


class EvaluationResult(BaseModel):
    thread_id: str
    candidate_id: str
    status: Literal["completed", "pending_review"]
    decision: Literal["shortlist", "human_review", "reject", "incomplete"] | None
    final_decision: Literal["shortlisted", "rejected"] | None = None
    # Ready-to-write value for the Google Sheet "status" column
    sheet_status: Literal["Scheduled", "Shortlisted", "Rejected", "Human Review", "Incomplete"]
    scores: ScoreBreakdown | None = None
    score_attempts: int = 0
    interview: InterviewOut | None = None
    messages: dict[str, str] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    hr_review: dict | None = None
    incomplete_reason: str | None = None
    llm_model: str | None = None
