"""ElevenLabs Agents: the AI interviewer calls the candidate on WhatsApp.

Flow
  n8n -> POST /voice/whatsapp-call          (API key)
         creates an interview link (tracking + browser fallback), then asks ElevenLabs to
         call the candidate. WhatsApp first sends a call-permission request; once the
         candidate taps "Allow", the agent calls them.
  ElevenLabs -> POST /elevenlabs/post-call  (HMAC-signed, public)
         transcript + our dynamic variables -> normalised -> n8n "voice-call-ended" ->
         the same evaluation pipeline as browser calls.

Signature scheme (copied from the official SDK, elevenlabs/webhooks_custom.py):
  header  ElevenLabs-Signature: t=<unix seconds>,v0=<hex>
  hex   = HMAC-SHA256(secret, f"{t}.{raw_body}")
  t must not be older than 30 minutes.
"""

import hashlib
import hmac
import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.interview.links import LinkStore
from app.interview.questions import interview_variables
from app.interview.routes import get_links, link_out
from app.security import require_api_key

log = structlog.get_logger()
router = APIRouter()

ELEVENLABS_API = "https://api.elevenlabs.io"
SIGNATURE_TOLERANCE_SECONDS = 30 * 60


# ----------------------------------------------------------------------------- helpers
def verify_signature(
    raw_body: bytes, header: str | None, secret: str, now: float | None = None
) -> bool:
    """Same checks as the SDK's construct_event: scheme, freshness, then constant-time compare."""
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, signature = parts.get("t"), parts.get("v0")
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if int(timestamp) < (now or time.time()) - SIGNATURE_TOLERANCE_SECONDS:
        return False
    message = f"{timestamp}.{raw_body.decode('utf-8')}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def wa_user_id(phone: str) -> str:
    """WhatsApp wants digits with country code: 0300-1234567 / +92 300 1234567 -> 923001234567."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:  # Pakistani local format
        digits = "92" + digits[1:]
    return digits


def transcript_text(turns: list[dict]) -> str:
    """ElevenLabs transcript turns -> the 'AI: ... / User: ...' text our agent reads."""
    lines = []
    for turn in turns or []:
        message = (turn.get("message") or "").strip()
        if message:
            lines.append(f"{'AI' if turn.get('role') == 'agent' else 'User'}: {message}")
    return "\n".join(lines)


def normalise_event(event: dict) -> dict[str, Any] | None:
    """post_call_transcription / call_initiation_failure -> the payload n8n's
    'voice-call-ended' webhook expects (same fields as a parsed Vapi report)."""
    kind, data = event.get("type"), event.get("data") or {}
    client = data.get("conversation_initiation_client_data") or {}
    variables = client.get("dynamic_variables") or {}
    if not variables.get("candidate_id"):
        return None  # not a screening call we started
    base = {
        "provider": "elevenlabs",
        "candidate_id": variables["candidate_id"],
        "interview_token": variables.get("interview_token"),
        "call_id": data.get("conversation_id"),
    }
    if kind == "call_initiation_failure":  # busy / no answer / permission never granted
        return base | {
            "transcript": "",
            "duration": 0,
            "call_time": "",
            "ended_reason": f"call_failed:{data.get('failure_reason', 'unknown')}",
        }
    if kind != "post_call_transcription":
        return None
    meta = data.get("metadata") or {}
    started = meta.get("start_time_unix_secs")
    return base | {
        "transcript": transcript_text(data.get("transcript") or []),
        "duration": meta.get("call_duration_secs"),
        "call_time": (datetime.fromtimestamp(started, UTC) + timedelta(hours=5)).strftime(
            "%a, %d %b %Y, %H:%M PKT"
        )
        if started
        else "",
        "ended_reason": data.get("status"),
    }


async def start_whatsapp_call(settings: Settings, to: str, variables: dict[str, str]) -> dict:
    """Ask ElevenLabs to call `to` on WhatsApp. Returns ElevenLabs' JSON or raises RuntimeError."""
    if not (
        settings.elevenlabs_api_key
        and settings.elevenlabs_agent_id
        and settings.meta_wa_phone_number_id
    ):
        raise RuntimeError(
            "ElevenLabs WhatsApp calling is not configured (API key / agent id / phone number id)"
        )
    body = {
        "whatsapp_phone_number_id": settings.meta_wa_phone_number_id,
        "whatsapp_user_id": to,
        "whatsapp_call_permission_request_template_name": settings.wa_call_permission_template,
        "whatsapp_call_permission_request_template_language_code": settings.wa_template_lang,
        "agent_id": settings.elevenlabs_agent_id,
        "conversation_initiation_client_data": {"dynamic_variables": variables},
    }
    headers = {"xi-api-key": settings.elevenlabs_api_key.get_secret_value()}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{ELEVENLABS_API}/v1/convai/whatsapp/outbound-call", json=body, headers=headers
        )
    if resp.status_code >= 300:
        raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("success") is False:
        raise RuntimeError(f"ElevenLabs: {data.get('message')}")
    return data


# ----------------------------------------------------------------------------- routes
SettingsDep = Annotated[Settings, Depends(get_settings)]
Links = Annotated[LinkStore, Depends(get_links)]


class WhatsAppCallRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    candidate_name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=7, max_length=30)


@router.post("/voice/whatsapp-call", tags=["voice"], dependencies=[Depends(require_api_key)])
async def whatsapp_call(req: WhatsAppCallRequest, links: Links, settings: SettingsDep) -> dict:
    """Create the interview link, then ask ElevenLabs to call the candidate on WhatsApp.
    Always returns the link (browser fallback); call_started tells n8n whether the call
    request went through, so it can fall back to the link if not."""
    ttl = timedelta(hours=settings.interview_link_ttl_hours)
    link = await links.create(req.candidate_id, req.candidate_name, req.role, ttl)
    variables = interview_variables(req.candidate_name, req.role, settings) | {
        # not used in the prompt; echoed back in the post-call webhook to identify the call
        "candidate_id": req.candidate_id,
        "interview_token": link.token,
    }
    out = link_out(link, settings).model_dump(mode="json")
    try:
        result = await start_whatsapp_call(settings, wa_user_id(req.phone), variables)
    except Exception as exc:
        log.warning("whatsapp_call_failed", candidate_id=req.candidate_id, error=str(exc))
        return out | {"call_started": False, "call_error": str(exc)[:300]}
    await links.mark_call_started(link.token)
    if result.get("conversation_id"):
        await links.set_call_id(link.token, result["conversation_id"])
    log.info(
        "whatsapp_call_requested",
        candidate_id=req.candidate_id,
        conversation_id=result.get("conversation_id"),
    )
    return out | {
        "call_started": True,
        "conversation_id": result.get("conversation_id"),
        "message": result.get("message"),
    }


@router.post("/elevenlabs/post-call", include_in_schema=False)
async def post_call(
    request: Request, background: BackgroundTasks, settings: SettingsDep, links: Links
) -> dict:
    from app.whatsapp import forward_to_n8n

    if not settings.elevenlabs_webhook_secret:
        raise HTTPException(503, "ELEVENLABS_WEBHOOK_SECRET not configured")
    raw = await request.body()  # raw bytes: the signature covers exactly these
    if not verify_signature(
        raw,
        request.headers.get("ElevenLabs-Signature"),
        settings.elevenlabs_webhook_secret.get_secret_value(),
    ):
        log.warning("elevenlabs_bad_signature")
        raise HTTPException(401, "Invalid signature")
    event = json.loads(raw)
    if settings.elevenlabs_agent_id and (event.get("data") or {}).get("agent_id") not in (
        None,
        settings.elevenlabs_agent_id,
    ):
        return {"status": "ignored", "reason": "other agent"}
    payload = normalise_event(event)
    if payload is None:
        return {"status": "ignored"}
    # The token links the call to the candidate we invited (dynamic variables alone aren't trusted)
    link = await links.get(payload["interview_token"]) if payload.get("interview_token") else None
    if link is None or link.candidate_id != payload["candidate_id"]:
        log.warning("elevenlabs_unknown_candidate", candidate_id=payload["candidate_id"])
        return {"status": "ignored", "reason": "unknown interview token"}
    background.add_task(forward_to_n8n, settings, "voice-call-ended", payload)
    log.info(
        "elevenlabs_call_ended",
        candidate_id=payload["candidate_id"],
        reason=payload.get("ended_reason"),
    )
    return {"status": "ok"}
