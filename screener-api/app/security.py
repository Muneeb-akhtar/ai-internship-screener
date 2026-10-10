"""Shared-secret authentication for machine-to-machine calls (n8n -> API).

n8n sends `X-API-Key: <SCREENER_API_KEY>`; endpoints that depend on
`require_api_key` reject any request without the right key.
Meta webhook signature checks (X-Hub-Signature-256) are added in Phase 5.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings

# auto_error=False -> we raise our own 401 with a clear message
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    api_key: Annotated[str | None, Depends(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    expected = settings.screener_api_key.get_secret_value()
    # compare_digest takes the same time whether the first or last char differs,
    # so an attacker can't guess the key one character at a time (timing attack).
    if api_key is None or not secrets.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
