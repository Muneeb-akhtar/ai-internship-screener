"""Signed, expiring recording links for HR."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import recordings
from app.config import Settings, get_settings
from app.main import app
from app.recordings import recording_link, verify

CALL = "01a12a33-248f-7bb1-9b88-e8f724dfa73c"
NOW = 1_800_000_000.0


@pytest.fixture
def settings() -> Settings:
    return Settings(vapi_private_key="vapi-private", public_base_url="https://screener.example.com")


def parts(link: str) -> tuple[str, int, str]:
    u = urlparse(link)
    q = parse_qs(u.query)
    return u.path.rsplit("/", 1)[1], int(q["exp"][0]), q["sig"][0]


def test_link_is_signed_and_expires_after_14_days(settings: Settings) -> None:
    link = recording_link(settings, CALL, now=NOW)
    assert link.startswith(f"https://screener.example.com/recordings/{CALL}?exp=")
    call_id, exp, sig = parts(link)
    assert verify(settings, call_id, exp, sig, now=NOW + 13 * 86400)
    assert not verify(settings, call_id, exp, sig, now=NOW + 15 * 86400)  # expired


def test_tampering_breaks_the_signature(settings: Settings) -> None:
    call_id, exp, sig = parts(recording_link(settings, CALL, now=NOW))
    assert not verify(settings, "another-call-id", exp, sig, now=NOW)  # other call
    assert not verify(settings, call_id, exp + 86400, sig, now=NOW)  # extended expiry


def test_no_link_without_vapi_private_key() -> None:
    assert recording_link(Settings(vapi_private_key=None), CALL) is None


def test_endpoint_redirects_to_fresh_audio(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_s: Settings, call_id: str) -> str:
        return f"https://storage.example/{call_id}.wav?signed=1"

    monkeypatch.setattr(recordings, "fetch_presigned_audio", fake_fetch)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        path = (
            urlparse(recording_link(settings, CALL)).path
            + "?"
            + urlparse(recording_link(settings, CALL)).query
        )
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == f"https://storage.example/{CALL}.wav?signed=1"

        bad = client.get(
            f"/recordings/{CALL}?exp=9999999999&sig={'0' * 32}", follow_redirects=False
        )
        assert bad.status_code == 403
    finally:
        app.dependency_overrides.clear()
