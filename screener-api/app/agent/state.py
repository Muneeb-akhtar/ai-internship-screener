"""LangGraph STATE for the Screening Decision Agent + the Pydantic schemas the LLM fills.

KEY CONCEPT - State:
  The state is one shared dict that flows through the graph. Each node receives
  the current state and returns ONLY the keys it wants to change; LangGraph
  merges that partial update into the state. After every node, the checkpointer
  saves a snapshot of the state to Postgres (that's what makes pause/resume and
  crash recovery possible).

KEY CONCEPT - Reducers:
  By default a returned key REPLACES the old value. `Annotated[list, operator.add]`
  tells LangGraph to APPEND instead - we use it for `score_history`, so every
  scoring attempt (including re-scores) is kept for the audit trail.
"""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# What the LLM must return (structured output). Field descriptions are sent
# to the model as part of the JSON schema, so they double as instructions.
# ---------------------------------------------------------------------------


class CandidateAnswers(BaseModel):
    """Facts pulled out of the transcript by the `extract_answers` node."""

    consent_given: bool = Field(
        description="True only if the candidate clearly agreed to an AI-led, recorded interview."
    )
    communication: str | None = Field(
        None, description="Summary of how the candidate introduced themselves / communicated."
    )
    technical: str | None = Field(
        None, description="The candidate's answer to the technical question."
    )
    problem_solving: str | None = Field(
        None, description="The candidate's answer to the problem-solving question."
    )
    motivation: str | None = Field(None, description="Why the candidate wants this internship.")
    availability: str | None = Field(
        None, description="What the candidate said about availability (dates, hours, duration)."
    )
    asked_for_human: bool = Field(
        False, description="True if the candidate asked to speak with a human / HR person."
    )
    candidate_questions: list[str] = Field(
        default_factory=list, description="Questions the candidate asked about the internship."
    )


class Scores(BaseModel):
    """Rubric output of the `score` node. Each dimension is 0-10 (total max 40)."""

    communication: int = Field(ge=0, le=10, description="Clarity, structure and confidence.")
    technical: int = Field(ge=0, le=10, description="Basic technical knowledge for the role.")
    problem_solving: int = Field(
        ge=0, le=10, description="Approach to the problem-solving question."
    )
    motivation: int = Field(ge=0, le=10, description="Genuine interest in the role and company.")
    available: bool = Field(description="Can the candidate meet the internship's time commitment?")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How confident you are in these scores given the evidence (0-1).",
    )
    reasoning: str = Field(
        description="Evidence-based justification citing what the candidate said, per dimension."
    )
    summary: str = Field(description="2-3 sentence neutral summary of the candidate for HR.")

    @property
    def total(self) -> int:
        return self.communication + self.technical + self.problem_solving + self.motivation


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

Decision = Literal["shortlist", "human_review", "reject", "incomplete"]


class Interview(TypedDict):
    start: str  # ISO 8601 with timezone, e.g. 2026-10-13T10:00:00+05:00
    end: str
    timezone: str
    meet_link: str | None
    calendar_event_id: str | None


class Messages(TypedDict, total=False):
    candidate: str  # what n8n sends the candidate (WhatsApp + email)
    hr: str  # what n8n sends HR


class HRReview(TypedDict, total=False):
    decision: Literal["APPROVE", "REJECT"]
    reviewer: str | None
    note: str | None


class ScreeningState(TypedDict, total=False):
    # ---- input (from POST /evaluate) ----
    candidate_id: str
    candidate_name: str
    candidate_email: str | None
    role: str
    transcript: str
    call_duration_seconds: float | None

    # ---- produced by the nodes ----
    answers: dict  # CandidateAnswers as a plain dict (JSON-friendly for the checkpointer)
    scores: dict  # latest Scores (+ "total")
    score_history: Annotated[list[dict], operator.add]  # every attempt, appended (reducer)
    score_attempts: int
    needs_rescore: bool
    decision: Decision  # what the AI recommends
    final_decision: Literal["shortlisted", "rejected"]  # outcome after (optional) HR review
    hr_review: HRReview
    interview: Interview | None
    messages: Messages
    flags: list[str]  # e.g. "asked_for_human", "transcript_too_short"
    incomplete_reason: str
