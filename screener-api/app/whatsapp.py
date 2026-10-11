"""Meta WhatsApp Cloud API webhook: verification, signature check, and routing of replies.

  GET  /whatsapp/webhook   Meta's one-time verification (hub.challenge handshake)
  POST /whatsapp/webhook   incoming messages + delivery statuses

Security:
- Every POST must carry X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(META_APP_SECRET, raw body).
  We recompute it over the RAW bytes (before JSON parsing) and compare in constant time,
  so nobody can fake an "APPROVE" by posting to this URL.
- Only messages from HR_WHATSAPP_NUMBER can approve/reject a candidate.

Routing (kept thin on purpose - n8n does the orchestration):
- HR "APPROVE <thread_id>" / "REJECT <thread_id> [note]"  -> n8n webhook "hr-review" -> /review
- anything else (HR typo, candidate replies)               -> n8n webhook "whatsapp-inbound"
  (n8n replies with help text, or flags the message for HR, e.g. "I want to talk to a human")
We answer Meta with 200 immediately and forward in the background (Meta retries slow webhooks).
"""

import asyncio
import hashlib
import hmac
import json
import re
from collections import OrderedDict
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings

log = structlog.get_logger()
router = APIRouter()

# "approve C002-c83ec8f0", "REJECT c002-c83ec8f0 weak technical answer"
REVIEW_RE = re.compile(
    r"^\s*(APPROVE|REJECT)\s+([A-Za-z0-9_.-]+-[0-9a-f]{8})\b\s*(.*)$", re.I | re.S
)

_seen: OrderedDict[str, None] = OrderedDict()  # recent message ids (Meta may deliver twice)
SEEN_MAX = 1000


def digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def valid_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def parse_review(text: str) -> dict[str, str] | None:
    match = REVIEW_RE.match(text or "")
    if not match:
        return None
    decision, thread_id, note = match.groups()
    return {"decision": decision.upper(), "thread_id": thread_id, "note": note.strip() or None}


def message_text(msg: dict) -> str:
    """Text of a WhatsApp message (plain text or a quick-reply button)."""
    if msg.get("type") == "text":
        return msg.get("text", {}).get("body", "")
    if msg.get("type") == "button":
        return msg.get("button", {}).get("text", "")
    if msg.get("type") == "interactive":
        inter = msg.get("interactive", {})
        return (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
    return ""


def route_messages(payload: dict, hr_number: str) -> list[tuple[str, dict[str, Any]]]:
    """Turn a Meta webhook payload into (n8n_webhook_path, body) pairs. Pure function -> easy to test."""
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                msg_id = msg.get("id", "")
                if msg_id in _seen:
                    continue
                _seen[msg_id] = None
                if len(_seen) > SEEN_MAX:
                    _seen.popitem(last=False)

                sender, text = msg.get("from", ""), message_text(msg)
                base = {
                    "from": sender,
                    "name": names.get(sender),
                    "text": text,
                    "message_id": msg_id,
                }
                review = (
                    parse_review(text)
                    if hr_number and digits(sender) == digits(hr_number)
                    else None
                )
                if review:
                    out.append(("hr-review", {**base, **review, "reviewer": f"whatsapp:{sender}"}))
                else:
                    kind = (
                        "hr_message" if digits(sender) == digits(hr_number) else "candidate_message"
                    )
                    out.append(
                        ("whatsapp-inbound", {**base, "kind": kind, "type": msg.get("type")})
                    )
            # value["statuses"] = delivery receipts (sent/delivered/read/failed): log failures only
            for status in value.get("statuses", []):
                if status.get("status") == "failed":
                    log.warning(
                        "whatsapp_delivery_failed",
                        to=status.get("recipient_id"),
                        errors=status.get("errors"),
                    )
    return out


async def forward_to_n8n(settings: Settings, path: str, body: dict) -> None:
    url = f"{settings.n8n_internal_url.rstrip('/')}/webhook/{path}"
    headers = {"X-API-Key": settings.screener_api_key.get_secret_value()}  # n8n checks this
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code < 500:
                log.info("whatsapp_forwarded", path=path, status=resp.status_code)
                return
        except httpx.HTTPError as exc:
            log.warning("whatsapp_forward_error", path=path, error=str(exc))
        await asyncio.sleep(2 * (attempt + 1))
    log.error("whatsapp_forward_failed", path=path)


SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/whatsapp/webhook", include_in_schema=False)
async def verify_webhook(
    settings: SettingsDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    expected = (
        settings.meta_webhook_verify_token.get_secret_value()
        if settings.meta_webhook_verify_token
        else None
    )
    if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
        log.info("whatsapp_webhook_verified")
        return PlainTextResponse(challenge or "")
    raise HTTPException(403, "Verification failed")


@router.post("/whatsapp/webhook", include_in_schema=False)
async def receive_webhook(
    request: Request, background: BackgroundTasks, settings: SettingsDep
) -> dict:
    if not settings.meta_app_secret:
        raise HTTPException(503, "META_APP_SECRET not configured")
    body = await request.body()  # RAW bytes: the signature is computed over exactly these
    if not valid_signature(
        settings.meta_app_secret.get_secret_value(),
        body,
        request.headers.get("X-Hub-Signature-256"),
    ):
        log.warning("whatsapp_bad_signature")
        raise HTTPException(401, "Invalid signature")
    for path, payload in route_messages(json.loads(body), settings.hr_whatsapp_number or ""):
        background.add_task(forward_to_n8n, settings, path, payload)
    return {"status": "ok"}
