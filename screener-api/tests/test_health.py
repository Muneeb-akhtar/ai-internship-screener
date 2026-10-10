"""Phase 1 tests: /health and API-key protection (database is mocked)."""

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.conftest import TEST_API_KEY


def test_health_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_db() -> str:
        return "ok"

    monkeypatch.setattr(main, "check_database", fake_db)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "X-Request-ID" in resp.headers


def test_health_db_down_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_db() -> str:
        return "error: OperationalError"

    monkeypatch.setattr(main, "check_database", fake_db)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
def test_auth_ping_rejects_bad_key(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/auth/ping", headers=headers).status_code == 401


def test_auth_ping_accepts_valid_key(client: TestClient) -> None:
    resp = client.get("/auth/ping", headers={"X-API-Key": TEST_API_KEY})
    assert resp.status_code == 200
