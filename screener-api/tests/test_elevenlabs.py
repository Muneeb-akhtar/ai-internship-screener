"""ElevenLabs WhatsApp calls: signature check (against the official SDK), payload
normalisation, the start-call endpoint and the post-call webhook."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import whatsapp
from app.config import Settings, get_settings
from app.interview.links import InMemoryLinkStore
from app.main import app
from app.voice import elevenlabs as el
from tests.conftest import TEST_API_KEY

SECRET = "wsec_test_secret"
AUTH = {"X-API-Key": TEST_API_KEY}


def sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    return (
        f"t={ts},v0="
        + hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    )


def event(
    kind: str = "post_call_transcription", candidate_id: str = "C300", token: str = "tok"
) -> dict:
    return {
        "type": kind,
        "event_timestamp": 1_791_000_000,
        "data": {
            "agent_id": "agent_123",
            "conversation_id": "conv_abc",
            "status": "done",
            "transcript": [
                {"role": "agent", "message": "Hi Sara! Do you agree to continue?"},
                {"role": "user", "message": "Yes, I agree."},
                {"role": "agent", "message": None},
            ],
            "metadata": {"call_duration_secs": 245, "start_time_unix_secs": 1_791_000_000},
            "conversation_initiation_client_data": {
                "dynamic_variables": {"candidate_id": candidate_id, "interview_token": token}
            },
            "failure_reason": "no-answer",
        },
    }


# ---------------------------------------------------------------- signature (vs. the SDK)
def test_signature_matches_official_sdk() -> None:
    from elevenlabs.webhooks_custom import WebhooksClient

    body = json.dumps(event()).encode()
    header = sign(body)
    assert el.verify_signature(body, header, SECRET)
    # the SDK accepts exactly what we accept
    sdk = WebhooksClient.__new__(WebhooksClient)
    assert (
        sdk.construct_event(rawBody=body.decode(), sig_header=header, secret=SECRET)["type"]
        == "post_call_transcription"
    )


@pytest.mark.parametrize(
    "header",
    [None, "", "t=123", "v0=abc", f"t={int(time.time())},v0=" + "0" * 64],
)
def test_bad_signatures_rejected(header: str | None) -> None:
    assert not el.verify_signature(b"{}", header, SECRET)


def test_old_signature_rejected() -> None:
    body = b'{"a":1}'
    assert not el.verify_signature(body, sign(body, ts=int(time.time()) - 31 * 60), SECRET)


# ---------------------------------------------------------------- normalisation
def test_transcript_and_payload_normalised() -> None:
    p = el.normalise_event(event())
    assert p["transcript"] == "AI: Hi Sara! Do you agree to continue?\nUser: Yes, I agree."
    assert p["candidate_id"] == "C300" and p["interview_token"] == "tok"
    assert p["call_id"] == "conv_abc" and p["duration"] == 245 and p["provider"] == "elevenlabs"
    assert p["call_time"].endswith("PKT")


def test_failed_call_becomes_empty_transcript() -> None:
    p = el.normalise_event(event("call_initiation_failure"))
    assert p["transcript"] == "" and p["ended_reason"] == "call_failed:no-answer"


def test_foreign_calls_ignored() -> None:
    e = event()
    e["data"]["conversation_initiation_client_data"]["dynamic_variables"] = {}
    assert el.normalise_event(e) is None


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("0300-1234567", "923001234567"),
        ("+92 300 1234567", "923001234567"),
        ("00923001234567", "923001234567"),
        ("447700900123", "447700900123"),
    ],
)
def test_wa_user_id(raw: str, want: str) -> None:
    assert el.wa_user_id(raw) == want


# ---------------------------------------------------------------- endpoints
@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = Settings(
        elevenlabs_webhook_secret=SECRET,
        elevenlabs_agent_id="agent_123",
        elevenlabs_api_key="xi_test",
        meta_wa_phone_number_id="100000000000001",
        public_base_url="https://screener.example.com",
        company_name="Acme",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.links = InMemoryLinkStore()
    calls, forwarded = [], []

    async def fake_call(_s, to, variables):
        calls.append((to, variables))
        return {"success": True, "message": "scheduled", "conversation_id": "conv_new"}

    async def fake_forward(_s, path, body):
        forwarded.append((path, body))

    monkeypatch.setattr(el, "start_whatsapp_call", fake_call)
    monkeypatch.setattr(whatsapp, "forward_to_n8n", fake_forward)
    client = TestClient(app)
    client.calls, client.forwarded = calls, forwarded
    yield client
    app.dependency_overrides.clear()
    app.state.links = None


def start(api: TestClient) -> dict:
    body = {
        "candidate_id": "C300",
        "candidate_name": "Sara Khan",
        "role": "Python Developer Intern",
        "phone": "0300-1234567",
    }
    resp = api.post("/voice/whatsapp-call", json=body, headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


def test_start_call_sends_script_variables(api: TestClient) -> None:
    data = start(api)
    assert data["call_started"] is True and data["conversation_id"] == "conv_new"
    assert data["url"].startswith("https://screener.example.com/interview/")  # browser fallback
    to, variables = api.calls[0]
    assert to == "923001234567"
    assert variables["candidate_name"] == "Sara" and variables["company_name"] == "Acme"
    assert "tuple" in variables["technical_question"]  # same fixed question bank as browser calls
    assert variables["candidate_id"] == "C300" and variables["interview_token"] == data["token"]
    status = api.get("/candidates/C300/interview-status", headers=AUTH).json()
    assert status["call_starts"] == 1 and status["vapi_call_id"] == "conv_new"


def test_start_call_failure_still_returns_link(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_):
        raise RuntimeError("Meta: account blocked")

    monkeypatch.setattr(el, "start_whatsapp_call", boom)
    data = start(api)
    assert data["call_started"] is False and "blocked" in data["call_error"]
    assert data["url"]


def test_start_call_requires_api_key(api: TestClient) -> None:
    assert api.post("/voice/whatsapp-call", json={}).status_code == 401


def test_post_call_forwards_verified_transcript(api: TestClient) -> None:
    token = start(api)["token"]
    body = json.dumps(event(token=token)).encode()
    resp = api.post(
        "/elevenlabs/post-call", content=body, headers={"ElevenLabs-Signature": sign(body)}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    path, payload = api.forwarded[0]
    assert path == "voice-call-ended" and payload["candidate_id"] == "C300"
    assert payload["transcript"].startswith("AI: Hi Sara!")


def test_post_call_rejects_bad_signature(api: TestClient) -> None:
    body = json.dumps(event()).encode()
    assert (
        api.post(
            "/elevenlabs/post-call",
            content=body,
            headers={"ElevenLabs-Signature": sign(body, "wrong")},
        ).status_code
        == 401
    )
    assert api.forwarded == []


def test_post_call_ignores_unknown_token(api: TestClient) -> None:
    body = json.dumps(event(token="not-ours")).encode()
    resp = api.post(
        "/elevenlabs/post-call", content=body, headers={"ElevenLabs-Signature": sign(body)}
    )
    assert resp.json()["status"] == "ignored" and api.forwarded == []
