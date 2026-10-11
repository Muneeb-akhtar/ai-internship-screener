"""One-click HR Approve/Reject links from email."""

from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app import whatsapp
from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes
from app.agent.service import ScreeningService
from app.config import Settings, get_settings
from app.hr_review import review_links, verify
from app.interview.links import InMemoryLinkStore
from app.main import app
from tests.conftest import TEST_API_KEY, FakeLLM, FakeScheduler, load_transcript, make_scores

SETTINGS = Settings(public_base_url="https://screener.example.com")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    graph = build_graph(
        ScreeningNodes(FakeLLM(scores=[make_scores(6, 6, 6, 6)]), FakeScheduler()), InMemorySaver()
    )
    app.state.service = ScreeningService(graph, audit=None, llm_model="fake")
    app.state.links = InMemoryLinkStore()
    forwarded: list = []

    async def fake_forward(_s, path, body):
        forwarded.append((path, body))

    monkeypatch.setattr(whatsapp, "forward_to_n8n", fake_forward)
    c = TestClient(app)
    c.forwarded = forwarded
    yield c
    app.dependency_overrides.clear()
    app.state.service = None
    app.state.links = None


def local(url: str) -> str:
    u = urlparse(url)
    return f"{u.path}?{u.query}"


def borderline(client: TestClient) -> dict:
    body = {
        "candidate_id": "C050",
        "candidate_name": "Bilal Ahmed",
        "role": "Python Developer Intern",
        "transcript": load_transcript("borderline"),
    }
    resp = client.post("/evaluate", json=body, headers={"X-API-Key": TEST_API_KEY})
    assert resp.status_code == 200
    return resp.json()


def test_pending_result_carries_signed_links(client: TestClient) -> None:
    data = borderline(client)
    assert data["status"] == "pending_review"
    assert set(data["review_links"]) == {"approve", "reject"}
    assert data["review_links"]["approve"].startswith(
        f"https://screener.example.com/hr/review/{data['thread_id']}?decision=APPROVE"
    )


def test_get_only_shows_confirmation(client: TestClient) -> None:
    data = borderline(client)
    page = client.get(local(data["review_links"]["approve"]))
    assert page.status_code == 200
    assert "Yes, approve" in page.text
    assert client.forwarded == []  # opening the link (e.g. by an email scanner) decides nothing


def test_post_records_decision_via_n8n(client: TestClient) -> None:
    data = borderline(client)
    resp = client.post(local(data["review_links"]["reject"]))
    assert resp.status_code == 200 and "Decision recorded" in resp.text
    path, body = client.forwarded[0]
    assert path == "hr-review"
    assert body["thread_id"] == data["thread_id"] and body["decision"] == "REJECT"


def test_tampered_link_is_refused(client: TestClient) -> None:
    data = borderline(client)
    approve = local(data["review_links"]["approve"])
    forged = approve.replace("decision=APPROVE", "decision=REJECT")  # signature no longer matches
    assert client.post(forged).status_code == 403
    assert client.forwarded == []


def test_links_expire() -> None:
    links = review_links(SETTINGS, "C1-12345678", now=1_800_000_000)
    q = dict(p.split("=") for p in urlparse(links["approve"]).query.split("&"))
    assert verify(
        SETTINGS, "C1-12345678", "APPROVE", int(q["exp"]), q["sig"], now=1_800_000_000 + 6 * 86400
    )
    assert not verify(
        SETTINGS, "C1-12345678", "APPROVE", int(q["exp"]), q["sig"], now=1_800_000_000 + 8 * 86400
    )
