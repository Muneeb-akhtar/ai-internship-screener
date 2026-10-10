"""Shared pytest fixtures. Sets safe fake settings so tests never need a real .env."""

import os

import pytest
from fastapi.testclient import TestClient

TEST_API_KEY = "test-api-key-0123456789"

# Must be set before app.config.get_settings() is first called
os.environ.update(
    {
        "APP_ENV": "test",
        "SCREENER_API_KEY": TEST_API_KEY,
        "SCREENER_DB_PASSWORD": "not-used-in-tests",
    }
)


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)
