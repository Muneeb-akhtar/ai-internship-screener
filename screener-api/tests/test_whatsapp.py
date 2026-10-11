"""Phase 5: Meta WhatsApp webhook - verification handshake, signatures, routing of replies."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import whatsapp
from app.config import Settings, get_settings
from app.main import app
from app.whatsapp import parse_review, route_messages, valid_signature

SECRET = "meta-app-secret"
HR = "923001234567"
CANDIDATE = "923339876543"


def payload(sender: str, text: str, msg_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Someone"}}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": msg_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


def signed(body: bytes) -> dict:
    return {
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }


@pytest.fixture(autouse=True)
def clear_seen() -> None:
    whatsapp._seen.clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(
        meta_app_secret=SECRET, meta_webhook_verify_token="verify-me", hr_whatsapp_number=f"+{HR}"
    )
    app.dependency_overrides[get_settings] = lambda: settings
    forwarded: list = []

    async def fake_forward(_s, path, body):
        forwarded.append((path, body))

    monkeypatch.setattr(whatsapp, "forward_to_n8n", fake_forward)
    c = TestClient(app)
    c.forwarded = forwarded
    yield c
    app.dependency_overrides.clear()


def test_verification_handshake(client: TestClient) -> None:
    ok = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
    )
    assert bad.status_code == 403


def test_rejects_missing_or_forged_signature(client: TestClient) -> None:
    body = json.dumps(payload(HR, "APPROVE C002-c83ec8f0")).encode()
    assert client.post("/whatsapp/webhook", content=body).status_code == 401
    forged = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
    assert client.post("/whatsapp/webhook", content=body, headers=forged).status_code == 401
    assert client.forwarded == []


def test_hr_approve_is_forwarded_to_review(client: TestClient) -> None:
    body = json.dumps(payload(HR, "approve C002-c83ec8f0 great attitude")).encode()
    assert client.post("/whatsapp/webhook", content=body, headers=signed(body)).status_code == 200
    path, data = client.forwarded[0]
    assert path == "hr-review"
    assert data["decision"] == "APPROVE" and data["thread_id"] == "C002-c83ec8f0"
    assert data["note"] == "great attitude"


def test_candidate_cannot_approve_themselves(client: TestClient) -> None:
    body = json.dumps(payload(CANDIDATE, "APPROVE C002-c83ec8f0")).encode()
    client.post("/whatsapp/webhook", content=body, headers=signed(body))
    path, data = client.forwarded[0]
    assert path == "whatsapp-inbound" and data["kind"] == "candidate_message"


def test_duplicate_deliveries_are_ignored() -> None:
    p = payload(HR, "REJECT C002-c83ec8f0", msg_id="wamid.dup")
    assert len(route_messages(p, HR)) == 1
    assert route_messages(p, HR) == []


def test_parse_review_variants() -> None:
    assert parse_review("Reject  C101-4f8573a8") == {
        "decision": "REJECT",
        "thread_id": "C101-4f8573a8",
        "note": None,
    }
    assert parse_review("approve please") is None
    assert parse_review("I approve C1-12345678") is None  # must start with the keyword


def test_signature_helper() -> None:
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert valid_signature("k", body, good)
    assert not valid_signature("k", body + b" ", good)  # any byte change breaks it
    assert not valid_signature("k", body, None)
