"""NODES of the Screening Decision Agent, plus the ROUTING functions used by conditional edges.

KEY CONCEPT - Node:
  A node is just a (sync or async) function: it takes the current state and returns a
  dict with the keys it wants to update. Nodes don't call each other - the graph's
  edges decide what runs next.

KEY CONCEPT - Conditional edge (routing function):
  A plain function that looks at the state and returns the NAME of the next node.
  E.g. `route_after_check` returns "score" (re-score), "reject", "request_review" or
  "shortlist". The routing logic is deterministic Python - the LLM scores, but our
  code (not the LLM) decides what happens with those scores.

KEY CONCEPT - interrupt():
  `human_review` calls interrupt(payload). LangGraph saves the state via the
  checkpointer and STOPS the run; /evaluate returns "pending_review". Later,
  POST /review/{thread_id} resumes the graph with Command(resume={...}); the node
  runs again FROM THE TOP and this time interrupt() returns HR's answer.
  => code before interrupt() runs twice, so keep it free of side effects.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agent import prompts
from app.agent.state import CandidateAnswers, Scores, ScreeningState
from app.llm import LLMClient
from app.tools.calendar import InterviewScheduler

log = structlog.get_logger()

# ---- Decision rules (total is out of 40) ----------------------------------
REJECT_BELOW = 20  # total < 20 -> reject
SHORTLIST_FROM = 28  # total >= 28 -> shortlist; 20-27 -> human review
MIN_CONFIDENCE = 0.7  # below this, re-score once (reflection loop)
MAX_SCORE_ATTEMPTS = 2  # first attempt + 1 retry

# ---- Incomplete-call detection ----------------------------------------------
MIN_CALL_SECONDS = 60
MIN_CANDIDATE_WORDS = 40
CANDIDATE_PREFIXES = ("user:", "candidate:", "customer:")
INTERVIEWER_PREFIXES = ("ai:", "assistant:", "bot:", "interviewer:")


def candidate_word_count(transcript: str) -> int:
    """Count words spoken by the candidate. Vapi transcripts look like
    'AI: ...\\nUser: ...'. If there are no speaker labels, count everything."""
    lines = [ln.strip() for ln in transcript.splitlines() if ln.strip()]
    labelled = [
        ln for ln in lines if ln.lower().startswith(CANDIDATE_PREFIXES + INTERVIEWER_PREFIXES)
    ]
    if not labelled:
        return len(transcript.split())
    candidate_lines = [
        ln.split(":", 1)[1] for ln in lines if ln.lower().startswith(CANDIDATE_PREFIXES)
    ]
    return sum(len(ln.split()) for ln in candidate_lines)


def _first_name(state: ScreeningState) -> str:
    return (state.get("candidate_name") or "there").split()[0]


def _add_flag(state: ScreeningState, flag: str) -> list[str]:
    flags = list(state.get("flags", []))
    return flags if flag in flags else [*flags, flag]


def _fmt_when(iso: str, tz_name: str) -> str:
    """'2026-10-13T10:00:00+05:00' -> 'Tuesday, 13 October 2026 at 10:00 AM (PKT)'.
    An ISO string only keeps the +05:00 offset, so re-attach the named zone for 'PKT'."""
    dt = datetime.fromisoformat(iso).astimezone(ZoneInfo(tz_name))
    return f"{dt.strftime('%A, %d %B %Y at %I:%M %p')} ({dt.tzname()})"


def _score_lines(scores: dict) -> str:
    return (
        f"Score: {scores['total']}/40 (confidence {scores['confidence']:.2f})\n"
        f"- Communication: {scores['communication']}/10\n"
        f"- Technical: {scores['technical']}/10\n"
        f"- Problem solving: {scores['problem_solving']}/10\n"
        f"- Motivation: {scores['motivation']}/10\n"
        f"- Available: {'yes' if scores['available'] else 'no'}"
    )


class ScreeningNodes:
    """Nodes are methods so they can share dependencies (LLM, scheduler) that are
    injected once - tests inject fakes, production injects the real ones."""

    def __init__(self, llm: LLMClient, scheduler: InterviewScheduler) -> None:
        self.llm = llm
        self.scheduler = scheduler

    # ------------------------------------------------------------------ guards
    async def check_transcript(self, state: ScreeningState) -> dict:
        """Cheap checks BEFORE spending LLM calls: was the call long enough to evaluate?"""
        duration = state.get("call_duration_seconds")
        if duration is not None and duration < MIN_CALL_SECONDS:
            return {"decision": "incomplete", "incomplete_reason": "call_too_short"}
        if candidate_word_count(state.get("transcript", "")) < MIN_CANDIDATE_WORDS:
            return {"decision": "incomplete", "incomplete_reason": "transcript_too_short"}
        return {}  # nothing to change - carry on

    async def extract_answers(self, state: ScreeningState) -> dict:
        """LLM call #1: transcript -> structured CandidateAnswers."""
        answers = await self.llm.structured(
            CandidateAnswers, prompts.extract_messages(state["role"], state["transcript"])
        )
        update: dict = {"answers": answers.model_dump()}
        if answers.asked_for_human:
            update["flags"] = _add_flag(state, "asked_for_human")
        if not answers.consent_given:
            update |= {"decision": "incomplete", "incomplete_reason": "no_consent"}
        return update

    async def incomplete(self, state: ScreeningState) -> dict:
        reason = state.get("incomplete_reason", "unknown")
        name = _first_name(state)
        if reason == "no_consent":
            candidate_msg = (
                f"Hi {name}, thanks for your time. Since the interview could not go ahead without "
                "consent, our HR team will contact you about other options. - The Hiring Team"
            )
        else:
            candidate_msg = (
                f"Hi {name}, it looks like your screening interview for the {state['role']} "
                "internship ended early. No problem - we'll send you a new link so you can try "
                "again. - The Hiring Team"
            )
        hr_msg = (
            f"Screening INCOMPLETE for {state.get('candidate_name')} ({state['candidate_id']}, "
            f"{state['role']}). Reason: {reason}."
        )
        return {"messages": {"candidate": candidate_msg, "hr": hr_msg}}

    # ----------------------------------------------------------------- scoring
    async def score(self, state: ScreeningState) -> dict:
        """LLM call #2 (and #3 on a re-score): apply the rubric with structured output."""
        attempt = state.get("score_attempts", 0) + 1
        previous = state.get("scores") if attempt > 1 else None
        scores = await self.llm.structured(
            Scores,
            prompts.score_messages(
                state["role"],
                state["transcript"],
                json.dumps(state.get("answers", {}), ensure_ascii=False),
                previous=previous,
            ),
        )
        record = scores.model_dump() | {"total": scores.total, "attempt": attempt}
        log.info("scored", attempt=attempt, total=scores.total, confidence=scores.confidence)
        # score_history uses the operator.add reducer -> this list is APPENDED
        return {"scores": record, "score_history": [record], "score_attempts": attempt}

    async def self_check(self, state: ScreeningState) -> dict:
        """Reflection: if the model isn't confident, re-score once; otherwise decide."""
        s = state["scores"]
        if s["confidence"] < MIN_CONFIDENCE and state["score_attempts"] < MAX_SCORE_ATTEMPTS:
            return {"needs_rescore": True}

        flags = state.get("flags", [])
        if not s["available"] or s["total"] < REJECT_BELOW:
            decision = "reject"
        elif s["total"] < SHORTLIST_FROM:
            decision = "human_review"
        else:
            decision = "shortlist"

        # Still unsure after the retry? Don't let the AI auto-decide - ask HR.
        if s["confidence"] < MIN_CONFIDENCE and decision != "human_review":
            decision = "human_review"
            flags = _add_flag(state, "low_confidence")
        return {"needs_rescore": False, "decision": decision, "flags": flags}

    # ------------------------------------------------------- human in the loop
    async def request_review(self, state: ScreeningState, config: RunnableConfig) -> dict:
        """Drafts the HR message BEFORE pausing. This is its own node so the draft is
        checkpointed (a node's updates are only saved when the node finishes)."""
        thread_id = config["configurable"]["thread_id"]
        s = state["scores"]
        hr_msg = (
            f"Borderline candidate - your decision needed\n"
            f"{state.get('candidate_name')} ({state['candidate_id']}) - {state['role']}\n"
            f"{_score_lines(s)}\n\nAI summary: {s['summary']}\n\n"
            f"Reply APPROVE {thread_id} or REJECT {thread_id}"
        )
        return {"messages": {"hr": hr_msg}}

    async def human_review(self, state: ScreeningState) -> dict:
        """Pauses the graph until HR answers (see module docstring)."""
        answer = interrupt(
            {
                "type": "hr_review",
                "candidate_id": state["candidate_id"],
                "summary": state["scores"]["summary"],
                "total": state["scores"]["total"],
                "allowed": ["APPROVE", "REJECT"],
            }
        )
        # Runs only after resume: validate HR's answer
        decision = str(answer.get("decision", "")).upper()
        if decision not in ("APPROVE", "REJECT"):
            raise ValueError(f"HR decision must be APPROVE or REJECT, got {decision!r}")
        return {
            "hr_review": {
                "decision": decision,
                "reviewer": answer.get("reviewer"),
                "note": answer.get("note"),
            }
        }

    # ---------------------------------------------------------------- outcomes
    async def shortlist(self, state: ScreeningState) -> dict:
        """Book the next free slot (tool call) and draft messages for candidate and HR."""
        s = state["scores"]
        booked = await self.scheduler.book_interview(
            candidate_name=state.get("candidate_name", ""),
            candidate_email=state.get("candidate_email"),
            role=state["role"],
            description=f"Internship interview ({state['role']}).\n\nAI screening summary: {s['summary']}",
        )
        name = _first_name(state)
        flags = state.get("flags", [])
        interview = None
        if booked:
            interview = {
                "start": booked.start.isoformat(),
                "end": booked.end.isoformat(),
                "timezone": str(booked.start.tzinfo),
                "meet_link": booked.meet_link,
                "calendar_event_id": booked.calendar_event_id,
            }
            when = _fmt_when(interview["start"], interview["timezone"])
            link = booked.meet_link or "(the meeting link will be shared separately)"
            candidate_msg = (
                f"Congratulations {name}! You've been shortlisted for the {state['role']} internship. "
                f"Your interview with our team is on {when}.\nJoin here: {link}\n"
                "Please reply if you need to reschedule. - The Hiring Team"
            )
            hr_when = f"Interview booked: {when}\nMeet: {link}"
        else:
            flags = [*flags, "no_slot_available"] if "no_slot_available" not in flags else flags
            candidate_msg = (
                f"Congratulations {name}! You've been shortlisted for the {state['role']} internship. "
                "Our team will contact you shortly to schedule your interview. - The Hiring Team"
            )
            hr_when = "No free slot found in the next working days - please schedule manually."

        reviewed = " (approved by HR)" if state.get("hr_review") else ""
        hr_msg = (
            f"SHORTLISTED{reviewed}: {state.get('candidate_name')} ({state['candidate_id']}) - "
            f"{state['role']}\n{_score_lines(s)}\n\nAI summary: {s['summary']}\n\n{hr_when}"
        )
        return {
            "final_decision": "shortlisted",
            "interview": interview,
            "messages": {"candidate": candidate_msg, "hr": hr_msg},
            "flags": flags,
        }

    async def reject(self, state: ScreeningState) -> dict:
        """LLM drafts a kind rejection; a template is used if the LLM call fails."""
        s = state.get("scores", {})
        name = _first_name(state)
        try:
            candidate_msg = await self.llm.text(
                prompts.rejection_messages(name, state["role"], s.get("summary", ""))
            )
        except Exception as exc:  # never let message drafting break the pipeline
            log.warning("rejection_draft_failed", error=str(exc))
            candidate_msg = prompts.fallback_rejection(name, state["role"])

        by = " by HR" if state.get("hr_review") else ""
        hr_msg = (
            f"REJECTED{by}: {state.get('candidate_name')} ({state['candidate_id']}) - {state['role']}\n"
            f"{_score_lines(s)}\n\nAI summary: {s.get('summary', '')}"
        )
        return {
            "final_decision": "rejected",
            "messages": {"candidate": candidate_msg.strip(), "hr": hr_msg},
        }


# ------------------------------------------------------------------ routers
def route_after_transcript_check(state: ScreeningState) -> str:
    return "incomplete" if state.get("decision") == "incomplete" else "extract_answers"


def route_after_extract(state: ScreeningState) -> str:
    return "incomplete" if state.get("decision") == "incomplete" else "score"


def route_after_check(state: ScreeningState) -> str:
    if state.get("needs_rescore"):
        return "score"  # <- the reflection loop: an edge pointing BACKWARDS
    return {"reject": "reject", "human_review": "request_review", "shortlist": "shortlist"}[
        state["decision"]
    ]


def route_after_review(state: ScreeningState) -> str:
    return "shortlist" if state["hr_review"]["decision"] == "APPROVE" else "reject"
