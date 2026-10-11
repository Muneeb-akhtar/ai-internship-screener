"""One-click HR decisions from email: signed Approve / Reject links.

The "Human Review" email to HR contains two links:
    {PUBLIC_BASE_URL}/hr/review/{thread_id}?decision=APPROVE&exp=...&sig=...
GET shows a small confirmation page (it does NOT decide anything: email scanners and link
previews open links automatically). Only the button on that page (POST) records the decision,
which is handed to the same n8n "hr-review" workflow that WhatsApp replies use.
Links are HMAC-signed per thread + decision and expire after 7 days.
"""

import hashlib
import hmac
import time
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse

from app.config import Settings, get_settings

log = structlog.get_logger()
router = APIRouter()

LINK_TTL_SECONDS = 7 * 24 * 3600
Decision = Literal["APPROVE", "REJECT"]


def _key(settings: Settings) -> bytes:
    return hashlib.sha256(
        b"hr-review-links:" + settings.screener_api_key.get_secret_value().encode()
    ).digest()


def _sign(settings: Settings, thread_id: str, decision: str, exp: int) -> str:
    msg = f"{thread_id}:{decision}:{exp}".encode()
    return hmac.new(_key(settings), msg, hashlib.sha256).hexdigest()[:32]


def review_links(settings: Settings, thread_id: str, now: float | None = None) -> dict[str, str]:
    exp = int((now or time.time()) + LINK_TTL_SECONDS)
    base = f"{settings.public_base_url.rstrip('/')}/hr/review/{thread_id}"
    return {
        d.lower(): f"{base}?decision={d}&exp={exp}&sig={_sign(settings, thread_id, d, exp)}"
        for d in ("APPROVE", "REJECT")
    }


def verify(
    settings: Settings, thread_id: str, decision: str, exp: int, sig: str, now: float | None = None
) -> bool:
    if (now or time.time()) > exp:
        return False
    return hmac.compare_digest(sig, _sign(settings, thread_id, decision, exp))


ThreadId = Annotated[str, PathParam(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _page(
    request: Request, title: str, text: str, status: int = 200, form: dict | None = None
) -> HTMLResponse:
    from app.interview.routes import templates

    return templates.TemplateResponse(
        request, "hr_review.html", {"title": title, "text": text, "form": form}, status_code=status
    )


async def _pending_candidate(request: Request, thread_id: str) -> tuple[bool, str]:
    """(still waiting for HR?, candidate label) - read from the agent's checkpoint."""
    service = getattr(request.app.state, "service", None)
    if service is None:
        return True, thread_id
    try:
        result = await service.get(thread_id)
    except Exception:
        return False, thread_id
    return result.status == "pending_review", f"{result.candidate_id}"


@router.get("/hr/review/{thread_id}", include_in_schema=False)
async def confirm_page(
    request: Request,
    thread_id: ThreadId,
    settings: SettingsDep,
    decision: Annotated[Decision, Query()],
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(min_length=32, max_length=32)],
) -> HTMLResponse:
    if not verify(settings, thread_id, decision, exp, sig):
        return _page(
            request, "Link expired or invalid", "This decision link is no longer valid.", 403
        )
    pending, who = await _pending_candidate(request, thread_id)
    if not pending:
        return _page(request, "Already decided", f"A decision for {who} has already been recorded.")
    verb = "Approve" if decision == "APPROVE" else "Reject"
    return _page(
        request,
        f"{verb} candidate {who}?",
        "Please confirm. The candidate and the Google Sheet will be updated automatically.",
        form={"decision": decision, "exp": exp, "sig": sig, "label": f"Yes, {verb.lower()}"},
    )


@router.post("/hr/review/{thread_id}", include_in_schema=False)
async def submit_decision(
    request: Request,
    thread_id: ThreadId,
    settings: SettingsDep,
    decision: Annotated[Decision, Query()],
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(min_length=32, max_length=32)],
) -> HTMLResponse:
    from app.whatsapp import forward_to_n8n

    if not verify(settings, thread_id, decision, exp, sig):
        return _page(
            request, "Link expired or invalid", "This decision link is no longer valid.", 403
        )
    pending, who = await _pending_candidate(request, thread_id)
    if not pending:
        return _page(request, "Already decided", f"A decision for {who} has already been recorded.")
    await forward_to_n8n(
        settings,
        "hr-review",
        {
            "thread_id": thread_id,
            "decision": decision,
            "reviewer": "email-link",
            "note": None,
            "from": "",
        },
    )
    log.info("hr_review_link_used", thread_id=thread_id, decision=decision)
    done = "approved - an interview is being scheduled" if decision == "APPROVE" else "rejected"
    return _page(
        request,
        "Decision recorded",
        f"Candidate {who} {done}. You'll receive a confirmation email shortly.",
    )
