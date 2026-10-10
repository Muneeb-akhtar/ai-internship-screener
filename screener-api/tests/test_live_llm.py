"""OPTIONAL live test: runs the 3 sample transcripts through the REAL LLM (uses API quota).

Skipped unless RUN_LIVE_LLM=1. Reads the key from the repo-root .env.
    $env:RUN_LIVE_LLM=1; pytest tests/test_live_llm.py -s; Remove-Item Env:RUN_LIVE_LLM

LLM output varies a little run to run, so assertions check the ordering and the clear
cases (strong > borderline > weak; weak is rejected) rather than exact numbers.
"""

import os
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes
from app.agent.service import ScreeningService
from app.config import Settings
from app.llm import build_llm
from app.schemas import EvaluateRequest
from tests.conftest import FakeScheduler, load_transcript

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1", reason="set RUN_LIVE_LLM=1 to call the real LLM"
    ),
]

ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


async def test_three_transcripts_with_real_llm() -> None:
    settings = Settings(_env_file=ROOT_ENV)  # picks up GOOGLE_API_KEY / LLM_PROVIDER / LLM_MODEL
    llm = build_llm(settings)
    service = ScreeningService(
        build_graph(ScreeningNodes(llm, FakeScheduler()), InMemorySaver()), llm_model=llm.model_name
    )

    results = {}
    for name in ("strong", "borderline", "weak"):
        results[name] = await service.evaluate(
            EvaluateRequest(
                candidate_id=f"LIVE-{name}",
                candidate_name={
                    "strong": "Sara Khan",
                    "borderline": "Bilal Ahmed",
                    "weak": "Kamran Ali",
                }[name],
                role="Python Developer Intern",
                transcript=load_transcript(name),
            )
        )
        r = results[name]
        s = r.scores
        print(
            f"\n[{name}] decision={r.decision} total={s.total}/40 "
            f"(C{s.communication} T{s.technical} P{s.problem_solving} M{s.motivation}) "
            f"available={s.available} confidence={s.confidence:.2f} attempts={r.score_attempts}"
            f"\n  summary: {s.summary}\n  candidate msg: {r.messages.get('candidate', '')[:160]}"
        )

    strong, borderline, weak = (results[n].scores.total for n in ("strong", "borderline", "weak"))
    assert strong > borderline > weak
    assert results["strong"].decision in ("shortlist", "human_review")
    assert results["weak"].decision == "reject"  # not available + weak answers
    assert results["weak"].scores.available is False
