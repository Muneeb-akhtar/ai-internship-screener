"""Phase 2: the Screening Decision Agent end to end, with a mocked LLM and scheduler.

Uses the in-memory checkpointer, so interrupt/resume works exactly like with Postgres.
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes, candidate_word_count
from app.agent.service import NotPendingReview, ScreeningService, ThreadNotFound
from app.schemas import EvaluateRequest, ReviewRequest
from tests.conftest import FakeLLM, FakeScheduler, load_transcript, make_answers, make_scores

pytestmark = pytest.mark.anyio


def make_service(llm: FakeLLM, scheduler: FakeScheduler | None = None) -> ScreeningService:
    graph = build_graph(ScreeningNodes(llm, scheduler or FakeScheduler()), InMemorySaver())
    return ScreeningService(graph, audit=None, llm_model=llm.model_name)


def request(transcript: str = "strong", **overrides) -> EvaluateRequest:
    data = {
        "candidate_id": "C001",
        "candidate_name": "Sara Khan",
        "candidate_email": "sara@example.com",
        "role": "Python Developer Intern",
        "transcript": load_transcript(transcript),
        "call_duration_seconds": 290,
    } | overrides
    return EvaluateRequest(**data)


# ------------------------------------------------------------ 3 sample transcripts
async def test_strong_candidate_is_shortlisted_and_booked() -> None:
    llm = FakeLLM(scores=[make_scores(9, 8, 8, 9)])  # total 34
    scheduler = FakeScheduler()
    result = await make_service(llm, scheduler).evaluate(request("strong"))

    assert result.status == "completed"
    assert result.decision == "shortlist"
    assert result.final_decision == "shortlisted"
    assert result.sheet_status == "Scheduled"
    assert result.scores.total == 34
    assert result.interview.meet_link == "https://meet.google.com/abc-defg-hij"
    assert result.interview.start == "2026-10-12T10:00:00+05:00"
    assert "Monday, 12 October 2026 at 10:00 AM (PKT)" in result.messages["candidate"]
    assert "34/40" in result.messages["hr"]
    assert llm.calls == ["CandidateAnswers", "Scores"]  # no re-score, no rejection draft
    assert len(scheduler.bookings) == 1


async def test_borderline_candidate_waits_for_hr_then_approved() -> None:
    llm = FakeLLM(scores=[make_scores(6, 5, 6, 6)])  # total 23
    service = make_service(llm)
    result = await service.evaluate(request("borderline", candidate_id="C002"))

    assert result.status == "pending_review"
    assert result.decision == "human_review"
    assert result.sheet_status == "Human Review"
    assert f"APPROVE {result.thread_id}" in result.messages["hr"]

    approved = await service.review(result.thread_id, ReviewRequest(decision="approve"))
    assert approved.status == "completed"
    assert approved.final_decision == "shortlisted"
    assert approved.hr_review["decision"] == "APPROVE"
    assert "approved by HR" in approved.messages["hr"]

    with pytest.raises(NotPendingReview):  # can't review twice
        await service.review(result.thread_id, ReviewRequest(decision="REJECT"))


async def test_borderline_candidate_rejected_by_hr() -> None:
    llm = FakeLLM(scores=[make_scores(6, 5, 6, 6)])
    service = make_service(llm)
    result = await service.evaluate(request("borderline"))
    rejected = await service.review(
        result.thread_id, ReviewRequest(decision="REJECT", reviewer="hr@x.com", note="weak tech")
    )
    assert rejected.final_decision == "rejected"
    assert rejected.sheet_status == "Rejected"
    assert rejected.hr_review == {"decision": "REJECT", "reviewer": "hr@x.com", "note": "weak tech"}
    assert "REJECTED by HR" in rejected.messages["hr"]


async def test_weak_candidate_is_rejected_with_kind_message() -> None:
    llm = FakeLLM(scores=[make_scores(3, 1, 2, 2, available=False)], rejection="Hi Kamran, thanks!")
    result = await make_service(llm).evaluate(request("weak", candidate_name="Kamran Ali"))
    assert result.decision == "reject"
    assert result.final_decision == "rejected"
    assert result.messages["candidate"] == "Hi Kamran, thanks!"
    assert result.interview is None


# ----------------------------------------------------------------- decision rules
@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ((5, 5, 5, 4), "reject"),  # 19
        ((5, 5, 5, 5), "human_review"),  # 20 (lower boundary)
        ((7, 7, 7, 6), "human_review"),  # 27 (upper boundary)
        ((7, 7, 7, 7), "shortlist"),  # 28
    ],
)
async def test_routing_thresholds(scores: tuple[int, int, int, int], expected: str) -> None:
    result = await make_service(FakeLLM(scores=[make_scores(*scores)])).evaluate(request())
    assert result.decision == expected


async def test_not_available_is_rejected_even_with_high_scores() -> None:
    llm = FakeLLM(scores=[make_scores(10, 10, 10, 10, available=False)])
    result = await make_service(llm).evaluate(request())
    assert result.decision == "reject"


# ------------------------------------------------------------- reflection loop
async def test_low_confidence_triggers_one_rescore() -> None:
    llm = FakeLLM(scores=[make_scores(8, 8, 8, 8, confidence=0.5), make_scores(8, 7, 8, 8)])
    result = await make_service(llm).evaluate(request())
    assert result.score_attempts == 2
    assert result.scores.total == 31  # the second (confident) attempt wins
    assert llm.calls == ["CandidateAnswers", "Scores", "Scores"]
    assert result.decision == "shortlist"


async def test_still_unsure_after_retry_goes_to_hr_not_auto_decision() -> None:
    llm = FakeLLM(
        scores=[make_scores(9, 9, 9, 9, confidence=0.4), make_scores(9, 9, 9, 9, confidence=0.5)]
    )
    result = await make_service(llm).evaluate(request())
    assert result.score_attempts == 2  # max one retry
    assert result.decision == "human_review"
    assert "low_confidence" in result.flags


# ---------------------------------------------------------------- edge cases
async def test_short_call_is_incomplete_without_llm_calls() -> None:
    llm = FakeLLM()
    result = await make_service(llm).evaluate(request(call_duration_seconds=25))
    assert result.decision == "incomplete"
    assert result.sheet_status == "Incomplete"
    assert result.incomplete_reason == "call_too_short"
    assert "new link" in result.messages["candidate"]
    assert llm.calls == []  # cheap check runs before any LLM call


async def test_tiny_transcript_is_incomplete() -> None:
    req = request(transcript="strong")
    req.transcript = "AI: Hello, do you consent?\nUser: Yes.\nAI: Hello?"
    req.call_duration_seconds = None
    result = await make_service(FakeLLM()).evaluate(req)
    assert result.incomplete_reason == "transcript_too_short"


async def test_no_consent_is_incomplete() -> None:
    llm = FakeLLM(answers=make_answers(consent=False))
    result = await make_service(llm).evaluate(request())
    assert result.decision == "incomplete"
    assert result.incomplete_reason == "no_consent"
    assert "Scores" not in llm.calls


async def test_asking_for_human_is_flagged() -> None:
    llm = FakeLLM(answers=make_answers(asked_for_human=True), scores=[make_scores(8, 8, 8, 8)])
    result = await make_service(llm).evaluate(request())
    assert "asked_for_human" in result.flags


async def test_no_free_slot_still_shortlists_and_flags() -> None:
    llm = FakeLLM(scores=[make_scores(9, 9, 9, 9)])
    result = await make_service(llm, FakeScheduler(available=False)).evaluate(request())
    assert result.sheet_status == "Shortlisted"
    assert "no_slot_available" in result.flags


async def test_rejection_falls_back_to_template_when_llm_fails() -> None:
    llm = FakeLLM(scores=[make_scores(2, 2, 2, 2)], rejection=RuntimeError("quota"))
    result = await make_service(llm).evaluate(request(candidate_name="Kamran Ali"))
    assert result.final_decision == "rejected"
    assert result.messages["candidate"].startswith("Hi Kamran, thank you")


async def test_same_call_id_is_idempotent() -> None:
    llm = FakeLLM(scores=[make_scores(9, 9, 9, 9)])  # only ONE score available
    scheduler = FakeScheduler()
    service = make_service(llm, scheduler)
    first = await service.evaluate(request(call_id="vapi-call-1"))
    second = await service.evaluate(request(call_id="vapi-call-1"))  # webhook retry
    assert first.thread_id == second.thread_id
    assert second.decision == "shortlist"
    assert len(scheduler.bookings) == 1  # not booked twice


async def test_unknown_thread() -> None:
    with pytest.raises(ThreadNotFound):
        await make_service(FakeLLM()).get("nope-123")


def test_candidate_word_count_uses_speaker_labels() -> None:
    assert candidate_word_count("AI: hello there friend\nUser: one two three") == 3
    assert candidate_word_count("no labels here at all") == 5
