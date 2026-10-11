"""Signed, expiring links that let HR listen to a candidate's call recording.

Why: speech-to-text makes mistakes, so a human should be able to hear the original audio
before deciding. Vapi's raw recording files are private, and their presigned URLs expire
within hours - too short for a WhatsApp/email message HR may open days later.

How:
  /evaluate puts   {PUBLIC_BASE_URL}/recordings/{call_id}?exp=<unix>&sig=<hmac>   in HR's message.
  When clicked, we verify the HMAC signature + expiry, ask Vapi for the call (server-side,
  with the private key) and redirect to a FRESH presigned audio URL.
The signature (HMAC-SHA256) means nobody can forge a link for another call or extend its expiry.
"""

import hashlib
import hmac
import time
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import RedirectResponse, Response

from app.config import Settings, get_settings

log = structlog.get_logger()
router = APIRouter()

LINK_TTL_SECONDS = 14 * 24 * 3600
VAPI_API = "https://api.vapi.ai"


def _key(settings: Settings) -> bytes:
    # Derived from the API key so there's no extra secret to manage; the "purpose" prefix
    # keeps these signatures useless for anything else.
    return hashlib.sha256(
        b"recording-links:" + settings.screener_api_key.get_secret_value().encode()
    ).digest()


def _sign(settings: Settings, call_id: str, exp: int) -> str:
    return hmac.new(_key(settings), f"{call_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def recording_link(settings: Settings, call_id: str, now: float | None = None) -> str | None:
    """Signed link for HR, or None when recordings can't be served (no Vapi private key)."""
    if not settings.vapi_private_key:
        return None
    exp = int((now or time.time()) + LINK_TTL_SECONDS)
    base = settings.public_base_url.rstrip("/")
    return f"{base}/recordings/{call_id}?exp={exp}&sig={_sign(settings, call_id, exp)}"


def verify(settings: Settings, call_id: str, exp: int, sig: str, now: float | None = None) -> bool:
    if (now or time.time()) > exp:
        return False
    return hmac.compare_digest(sig, _sign(settings, call_id, exp))


async def fetch_presigned_audio(settings: Settings, call_id: str) -> str | None:
    """Ask Vapi for the call and return a fresh presigned recording URL (mono preferred)."""
    assert settings.vapi_private_key is not None
    headers = {
        "Authorization": f"Bearer {settings.vapi_private_key.get_secret_value()}",
        "User-Agent": "ai-internship-screener/0.1",  # Vapi's Cloudflare blocks generic agents
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{VAPI_API}/call/{call_id}", headers=headers)
    if resp.status_code != 200:
        log.warning("recording_fetch_failed", call_id=call_id, status=resp.status_code)
        return None
    artifact = resp.json().get("artifact") or {}
    return artifact.get("presignedMonoUrl") or artifact.get("presignedStereoUrl")


@router.get("/recordings/{call_id}", include_in_schema=False)
async def recording(
    request: Request,
    call_id: Annotated[str, PathParam(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9-]+$")],
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(min_length=32, max_length=32)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    from app.interview.routes import _message_page  # shared friendly error page

    if not verify(settings, call_id, exp, sig):
        return _message_page(
            request, "Link expired or invalid", "This recording link is no longer valid.", 403
        )
    if not settings.vapi_private_key:
        return _message_page(request, "Not available", "Recordings are not configured.", 503)
    url = await fetch_presigned_audio(settings, call_id)
    if not url:
        return _message_page(
            request, "Recording not found", "No recording is available for this call.", 404
        )
    log.info("recording_opened", call_id=call_id)
    return RedirectResponse(url, status_code=307)  # browser plays/downloads the audio directly
