"""Audit log: one row per evaluation in the `evaluations` table.

Transparency principle: every candidate's scores, every scoring attempt with its
reasoning, the AI decision, and HR's final call are stored and queryable, e.g.
  SELECT candidate_id, decision, final_decision, total, summary FROM evaluations;
(The LangGraph checkpoint tables hold the full step-by-step state as well.)
"""

import json

import structlog
from psycopg_pool import AsyncConnectionPool

from app.schemas import EvaluationResult

log = structlog.get_logger()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evaluations (
    thread_id       TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    candidate_name  TEXT,
    role            TEXT,
    status          TEXT NOT NULL,
    decision        TEXT,
    final_decision  TEXT,
    total           INTEGER,
    confidence      REAL,
    scores          JSONB,
    score_history   JSONB,
    answers         JSONB,
    flags           JSONB,
    hr_review       JSONB,
    interview       JSONB,
    llm_model       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evaluations_candidate_idx ON evaluations (candidate_id);
ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS recording_url TEXT;
"""

UPSERT_SQL = """
INSERT INTO evaluations (thread_id, candidate_id, candidate_name, role, status, decision,
    final_decision, total, confidence, scores, score_history, answers, flags, hr_review,
    interview, llm_model, recording_url)
VALUES (%(thread_id)s, %(candidate_id)s, %(candidate_name)s, %(role)s, %(status)s, %(decision)s,
    %(final_decision)s, %(total)s, %(confidence)s, %(scores)s, %(score_history)s, %(answers)s,
    %(flags)s, %(hr_review)s, %(interview)s, %(llm_model)s, %(recording_url)s)
ON CONFLICT (thread_id) DO UPDATE SET
    status = EXCLUDED.status, decision = EXCLUDED.decision,
    final_decision = EXCLUDED.final_decision, total = EXCLUDED.total,
    confidence = EXCLUDED.confidence, scores = EXCLUDED.scores,
    score_history = EXCLUDED.score_history, answers = EXCLUDED.answers,
    flags = EXCLUDED.flags, hr_review = EXCLUDED.hr_review, interview = EXCLUDED.interview,
    llm_model = EXCLUDED.llm_model, recording_url = EXCLUDED.recording_url, updated_at = now();
"""


def _json(value: object) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class AuditLog:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        # One statement per execute(): the pool uses prepared statements
        # (prepare_threshold=0), and those can't contain several commands.
        async with self._pool.connection() as conn:
            for statement in filter(str.strip, SCHEMA_SQL.split(";")):
                await conn.execute(statement)

    async def record(self, result: EvaluationResult, state: dict) -> None:
        """Insert or update the row for this evaluation. Failures are logged, not raised:
        the evaluation itself is already safe in the checkpoint tables."""
        scores = result.scores
        params = {
            "thread_id": result.thread_id,
            "candidate_id": result.candidate_id,
            "candidate_name": state.get("candidate_name"),
            "role": state.get("role"),
            "status": result.sheet_status,
            "decision": result.decision,
            "final_decision": result.final_decision,
            "total": scores.total if scores else None,
            "confidence": scores.confidence if scores else None,
            "scores": _json(scores.model_dump() if scores else None),
            "score_history": _json(state.get("score_history")),
            "answers": _json(state.get("answers")),
            "flags": _json(result.flags),
            "hr_review": _json(result.hr_review),
            "interview": _json(result.interview.model_dump() if result.interview else None),
            "llm_model": result.llm_model,
            "recording_url": result.recording_url,
        }
        try:
            async with self._pool.connection() as conn:
                await conn.execute(UPSERT_SQL, params)
        except Exception as exc:
            log.error("audit_write_failed", thread_id=result.thread_id, error=str(exc))
