"""Phase 2: HTTP layer for /evaluate, /review, /evaluations (agent mocked, no DB)."""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes
from app.agent.service import ScreeningService
from app.interview.links import InMemoryLinkStore
from app.main import app
from tests.conftest import TEST_API_KEY, FakeLLM, FakeScheduler, load_transcript, make_scores

AUTH = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def api(request: pytest.FixtureRequest) -> TestClient:
    """TestClient with a fake agent injected (lifespan is not run, so no DB is needed)."""
    scores = getattr(request, "param", [make_scores(6, 6, 6, 6)])  # default: borderline
    graph = build_graph(ScreeningNodes(FakeLLM(scores=scores), FakeScheduler()), InMemorySaver())
    app.state.service = ScreeningService(graph, audit=None, llm_model="fake-llm")
    app.state.links = InMemoryLinkStore()
    yield TestClient(app)
    app.state.service = None
    app.state.links = None


def body(**overrides) -> dict:
    return {
        "candidate_id": "C010",
        "candidate_name": "Sara Khan",
        "role": "Python Developer Intern",
        "transcript": load_transcript("strong"),
        "call_id": "call-abc",
    } | overrides


def test_evaluate_requires_api_key(api: TestClient) -> None:
    assert api.post("/evaluate", json=body()).status_code == 401


def test_evaluate_validates_input(api: TestClient) -> None:
    resp = api.post("/evaluate", json=body(candidate_id="bad id; drop"), headers=AUTH)
    assert resp.status_code == 422


def test_full_review_flow_over_http(api: TestClient) -> None:
    resp = api.post("/evaluate", json=body(), headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_review"
    thread_id = data["thread_id"]

    got = api.get(f"/evaluations/{thread_id}", headers=AUTH).json()
    assert got["sheet_status"] == "Human Review"

    done = api.post(f"/review/{thread_id}", json={"decision": "approve"}, headers=AUTH).json()
    assert done["final_decision"] == "shortlisted"
    assert done["sheet_status"] == "Scheduled"

    again = api.post(f"/review/{thread_id}", json={"decision": "APPROVE"}, headers=AUTH)
    assert again.status_code == 409


def test_review_unknown_thread_is_404(api: TestClient) -> None:
    resp = api.post("/review/C999-deadbeef", json={"decision": "APPROVE"}, headers=AUTH)
    assert resp.status_code == 404


def test_review_rejects_invalid_decision(api: TestClient) -> None:
    resp = api.post("/review/C999-deadbeef", json={"decision": "MAYBE"}, headers=AUTH)
    assert resp.status_code == 422


def test_evaluate_returns_503_when_agent_not_configured(client: TestClient) -> None:
    app.state.service = None
    assert client.post("/evaluate", json=body(), headers=AUTH).status_code == 503
