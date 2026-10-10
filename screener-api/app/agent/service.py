"""Runs the graph for the HTTP layer: start an evaluation, resume after HR review, read status.

Thread ids: one LangGraph thread per evaluation, e.g. "C001-3fa2b9c1". When n8n passes
Vapi's call_id, the id is derived from it, so a retried webhook maps to the SAME thread:
  - finished already  -> return the stored result (no duplicate LLM calls / bookings)
  - crashed midway    -> resume from the last checkpoint (ainvoke(None, ...))
  - waiting for HR    -> return pending_review
"""

import hashlib
import uuid
from typing import Any

import structlog
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from app.audit import AuditLog
from app.schemas import EvaluateRequest, EvaluationResult, ReviewRequest

log = structlog.get_logger()


class ThreadNotFound(Exception):
    pass


class NotPendingReview(Exception):
    pass


def make_thread_id(candidate_id: str, call_id: str | None) -> str:
    suffix = hashlib.sha256(call_id.encode()).hexdigest()[:8] if call_id else uuid.uuid4().hex[:8]
    return f"{candidate_id}-{suffix}"


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _sheet_status(values: dict, pending: bool) -> str:
    if pending:
        return "Human Review"
    if values.get("decision") == "incomplete":
        return "Incomplete"
    if values.get("final_decision") == "shortlisted":
        return "Scheduled" if values.get("interview") else "Shortlisted"
    return "Rejected"


class ScreeningService:
    def __init__(
        self, graph: CompiledStateGraph, audit: AuditLog | None = None, llm_model: str | None = None
    ) -> None:
        self.graph = graph
        self.audit = audit
        self.llm_model = llm_model

    # ------------------------------------------------------------------ public
    async def evaluate(self, req: EvaluateRequest) -> EvaluationResult:
        thread_id = make_thread_id(req.candidate_id, req.call_id)
        config = _config(thread_id)
        log_ = log.bind(thread_id=thread_id, candidate_id=req.candidate_id)

        snapshot = await self.graph.aget_state(config)
        if snapshot.values:  # this call was seen before (webhook retry)
            if snapshot.next and not snapshot.interrupts:
                log_.info("evaluation_resuming_after_failure", next=snapshot.next)
                await self.graph.ainvoke(None, config)  # continue from last checkpoint
            else:
                log_.info("evaluation_already_exists")
                return self._result(thread_id, await self.graph.aget_state(config))
        else:
            log_.info("evaluation_started")
            await self.graph.ainvoke(
                {
                    "candidate_id": req.candidate_id,
                    "candidate_name": req.candidate_name,
                    "candidate_email": req.candidate_email,
                    "role": req.role,
                    "transcript": req.transcript,
                    "call_duration_seconds": req.call_duration_seconds,
                    "flags": [],
                    "score_attempts": 0,
                },
                config,
            )
        return await self._finish(thread_id)

    async def review(self, thread_id: str, req: ReviewRequest) -> EvaluationResult:
        config = _config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            raise ThreadNotFound(thread_id)
        if not snapshot.interrupts:
            raise NotPendingReview(thread_id)
        log.info("hr_review_received", thread_id=thread_id, decision=req.decision)
        # Command(resume=...) becomes the return value of interrupt() inside human_review
        await self.graph.ainvoke(Command(resume=req.model_dump()), config)
        return await self._finish(thread_id)

    async def get(self, thread_id: str) -> EvaluationResult:
        snapshot = await self.graph.aget_state(_config(thread_id))
        if not snapshot.values:
            raise ThreadNotFound(thread_id)
        return self._result(thread_id, snapshot)

    # ----------------------------------------------------------------- helpers
    async def _finish(self, thread_id: str) -> EvaluationResult:
        snapshot = await self.graph.aget_state(_config(thread_id))
        result = self._result(thread_id, snapshot)
        log.info(
            "evaluation_result",
            thread_id=thread_id,
            status=result.status,
            decision=result.decision,
            total=result.scores.total if result.scores else None,
        )
        if self.audit:
            await self.audit.record(result, snapshot.values)
        return result

    def _result(self, thread_id: str, snapshot: StateSnapshot) -> EvaluationResult:
        v = snapshot.values
        pending = bool(snapshot.interrupts)
        scores = v.get("scores")
        return EvaluationResult(
            thread_id=thread_id,
            candidate_id=v["candidate_id"],
            status="pending_review" if pending else "completed",
            decision=v.get("decision"),
            final_decision=v.get("final_decision"),
            sheet_status=_sheet_status(v, pending),
            scores={k: scores[k] for k in scores if k != "attempt"} if scores else None,
            score_attempts=v.get("score_attempts", 0),
            interview=v.get("interview"),
            messages=dict(v.get("messages", {})),
            flags=list(v.get("flags", [])),
            hr_review=v.get("hr_review"),
            incomplete_reason=v.get("incomplete_reason"),
            llm_model=self.llm_model,
        )
