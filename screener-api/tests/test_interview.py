"""Phase 3: interview links, the Vapi page, attempt limits and token checks in /evaluate."""

import json
import re

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes
from app.agent.service import ScreeningService
from app.config import Settings, get_settings
from app.interview.links import InMemoryLinkStore
from app.interview.questions import GENERIC, questions_for_role
from app.main import app
from tests.conftest import TEST_API_KEY, FakeLLM, FakeScheduler, load_transcript, make_scores

AUTH = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def api() -> TestClient:
    settings = Settings(
        vapi_public_key="pk_test_public",
        vapi_assistant_id="asst_123",
        company_name="Acme",
        public_base_url="https://screener.example.com",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.links = InMemoryLinkStore()
    graph = build_graph(
        ScreeningNodes(FakeLLM(scores=[make_scores(9, 9, 9, 9)]), FakeScheduler()), InMemorySaver()
    )
    app.state.service = ScreeningService(graph, audit=None, llm_model="fake-llm")
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.state.service = None
    app.state.links = None


def create_link(api: TestClient, cid: str = "C100") -> dict:
    resp = api.post(
        "/interview-links",
        json={
            "candidate_id": cid,
            "candidate_name": "Sara Khan",
            "role": "Python Developer Intern",
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    return resp.json()


def page_config(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="config">(.*?)</script>', html, re.S)
    assert match, "config block missing"
    return json.loads(match.group(1))


def test_create_link_requires_api_key(api: TestClient) -> None:
    body = {"candidate_id": "C1", "candidate_name": "A", "role": "Intern"}
    assert api.post("/interview-links", json=body).status_code == 401


def test_link_url_uses_public_base_url(api: TestClient) -> None:
    link = create_link(api)
    assert link["url"] == f"https://screener.example.com/interview/{link['token']}"
    assert link["status"] == "sent"
    assert len(link["token"]) >= 30  # unguessable


def test_page_renders_vapi_config_and_marks_opened(api: TestClient) -> None:
    link = create_link(api)
    resp = api.get(f"/interview/{link['token']}")
    assert resp.status_code == 200
    assert "Hi Sara" in resp.text
    cfg = page_config(resp.text)
    assert cfg["publicKey"] == "pk_test_public"
    assert cfg["assistantId"] == "asst_123"
    values = cfg["overrides"]["variableValues"]
    assert values["technical_question"] == questions_for_role("Python Developer Intern").technical
    assert values["company_name"] == "Acme"
    assert cfg["overrides"]["metadata"] == {
        "interview_token": link["token"],
        "candidate_id": "C100",
    }
    # Secrets must never reach the browser
    assert TEST_API_KEY not in resp.text
    status = api.get("/candidates/C100/interview-status", headers=AUTH).json()
    assert status["status"] == "opened"


def test_unknown_link_shows_friendly_404(api: TestClient) -> None:
    resp = api.get("/interview/" + "x" * 32)
    assert resp.status_code == 404
    assert "not valid" in resp.text


def test_call_attempts_are_limited(api: TestClient) -> None:
    token = create_link(api)["token"]
    events = f"/interview/{token}/events"
    assert api.post(events, json={"event": "start-requested"}).status_code == 204
    assert api.post(events, json={"event": "call-started", "call_id": "call-1"}).status_code == 204
    assert api.post(events, json={"event": "start-requested"}).status_code == 204  # retry
    assert api.post(events, json={"event": "start-requested"}).status_code == 409  # 3rd: refused
    status = api.get("/candidates/C100/interview-status", headers=AUTH).json()
    assert status["call_starts"] == 2
    assert status["vapi_call_id"] == "call-1"
    assert status["status"] == "attempts_used"
    assert "No attempts left" in api.get(f"/interview/{token}").text


def evaluate_body(cid: str, token: str | None) -> dict:
    return {
        "candidate_id": cid,
        "candidate_name": "Sara Khan",
        "role": "Python Developer Intern",
        "transcript": load_transcript("strong"),
        "call_id": "call-xyz",
        "interview_token": token,
    }


def test_evaluate_rejects_token_of_another_candidate(api: TestClient) -> None:
    token = create_link(api, "C100")["token"]
    resp = api.post("/evaluate", json=evaluate_body("C999", token), headers=AUTH)
    assert resp.status_code == 403


def test_evaluate_with_token_completes_the_link(api: TestClient) -> None:
    token = create_link(api, "C100")["token"]
    resp = api.post("/evaluate", json=evaluate_body("C100", token), headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["decision"] == "shortlist"
    status = api.get("/candidates/C100/interview-status", headers=AUTH).json()
    assert status["status"] == "completed"
    assert status["thread_id"] == resp.json()["thread_id"]
    assert "already completed" in api.get(f"/interview/{token}").text


def test_page_says_not_configured_without_vapi_keys(api: TestClient) -> None:
    token = create_link(api)["token"]
    app.dependency_overrides[get_settings] = lambda: Settings(vapi_public_key=None)
    resp = api.get(f"/interview/{token}")
    assert resp.status_code == 503


def test_question_bank_matches_role_keywords() -> None:
    assert "tuple" in questions_for_role("Python Developer Intern").technical
    assert "median" in questions_for_role("Data Analyst Intern").technical
    assert questions_for_role("Office Assistant") == GENERIC
