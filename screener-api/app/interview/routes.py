"""HTTP routes for the candidate interview.

  POST /interview-links                         (n8n, API key)  create a link for a candidate
  GET  /candidates/{candidate_id}/interview-status (n8n, API key) opened? started? done? (reminders)
  GET  /interview/{token}                       (public)        the page that runs the Vapi browser call
  POST /interview/{token}/events                (public)        page reports "call-start" + Vapi call id

Public routes only work with a valid, unexpired token and only update timestamps.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.interview.links import MAX_CALL_STARTS, InterviewLink, LinkStore
from app.interview.questions import interview_variables
from app.security import require_api_key

log = structlog.get_logger()
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

Token = Annotated[str, PathParam(min_length=20, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_links(request: Request) -> LinkStore:
    store = getattr(request.app.state, "links", None)
    if store is None:
        raise HTTPException(503, "Link store not available")
    return store


Links = Annotated[LinkStore, Depends(get_links)]


# ------------------------------------------------------------------ models
class CreateLinkRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    candidate_name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    ttl_hours: int | None = Field(None, ge=1, le=24 * 14)


class LinkOut(BaseModel):
    candidate_id: str
    candidate_name: str
    role: str
    token: str
    url: str
    status: str
    created_at: datetime
    expires_at: datetime
    opened_at: datetime | None
    call_started_at: datetime | None
    call_starts: int
    vapi_call_id: str | None
    evaluated_at: datetime | None
    thread_id: str | None


class PageEvent(BaseModel):
    # start-requested: sent BEFORE starting Vapi -> reserves one of the MAX_CALL_STARTS attempts
    # call-started:    sent once Vapi returns the call id
    event: Literal["start-requested", "call-started"]
    call_id: str | None = Field(None, max_length=200)


def link_out(link: InterviewLink, settings: Settings) -> LinkOut:
    return LinkOut(
        candidate_id=link.candidate_id,
        candidate_name=link.candidate_name,
        role=link.role,
        token=link.token,
        url=f"{settings.public_base_url.rstrip('/')}/interview/{link.token}",
        status=link.state(),
        created_at=link.created_at,
        expires_at=link.expires_at,
        opened_at=link.opened_at,
        call_started_at=link.call_started_at,
        call_starts=link.call_starts,
        vapi_call_id=link.vapi_call_id,
        evaluated_at=link.evaluated_at,
        thread_id=link.thread_id,
    )


# ------------------------------------------------------------- n8n routes
@router.post("/interview-links", tags=["interview"], dependencies=[Depends(require_api_key)])
async def create_link(req: CreateLinkRequest, links: Links, settings: SettingsDep) -> LinkOut:
    ttl = timedelta(hours=req.ttl_hours or settings.interview_link_ttl_hours)
    link = await links.create(req.candidate_id, req.candidate_name, req.role, ttl)
    log.info("interview_link_created", candidate_id=req.candidate_id)
    return link_out(link, settings)


@router.get(
    "/candidates/{candidate_id}/interview-status",
    tags=["interview"],
    dependencies=[Depends(require_api_key)],
)
async def interview_status(candidate_id: str, links: Links, settings: SettingsDep) -> LinkOut:
    link = await links.latest_for_candidate(candidate_id)
    if link is None:
        raise HTTPException(404, "No interview link for this candidate")
    return link_out(link, settings)


# ----------------------------------------------------------- public routes
@router.get("/interview/{token}", response_class=HTMLResponse, include_in_schema=False)
async def interview_page(
    token: Token, request: Request, links: Links, settings: SettingsDep
) -> HTMLResponse:
    link = await links.get(token)
    if link is None:
        return _message_page(
            request,
            "Link not found",
            "This interview link is not valid. Please use the link from your message.",
            404,
        )
    state = link.state()
    if state == "completed":
        return _message_page(
            request,
            "Interview completed",
            "Thank you! You've already completed your screening interview. Our HR team will be in touch.",
        )
    if state == "expired":
        return _message_page(
            request,
            "Link expired",
            "This interview link has expired. Please reply to your invitation email and we'll send you a new one.",
            410,
        )
    if state == "attempts_used":
        return _message_page(
            request,
            "No attempts left",
            f"This link has been used for {MAX_CALL_STARTS} calls already. Our HR team will contact you.",
            410,
        )
    if not (settings.vapi_public_key and settings.vapi_assistant_id):
        return _message_page(
            request,
            "Not available yet",
            "Interviews are not configured yet. Please try again later.",
            503,
        )

    await links.mark_opened(token)
    # Everything here ends up in the browser - never put secrets in it.
    config = {
        "publicKey": settings.vapi_public_key,
        "assistantId": settings.vapi_assistant_id,
        "eventsUrl": f"/interview/{token}/events",
        "maxMinutes": 6,
        "overrides": {
            "variableValues": interview_variables(link.candidate_name, link.role, settings),
            # Echoed back in Vapi's end-of-call report; n8n passes interview_token to
            # /evaluate, which verifies it against the DB (metadata itself isn't trusted).
            "metadata": {"interview_token": token, "candidate_id": link.candidate_id},
        },
    }
    return templates.TemplateResponse(
        request,
        "interview.html",
        {
            "name": link.candidate_name.split()[0],
            "role": link.role,
            "company": settings.company_name,
            "config": config,
        },
    )


@router.post("/interview/{token}/events", include_in_schema=False, status_code=204)
async def interview_event(token: Token, event: PageEvent, links: Links) -> None:
    link = await links.get(token)
    if link is None:
        raise HTTPException(404, "Unknown link")
    if event.event == "start-requested":
        if not link.can_start_call(datetime.now(UTC)):
            raise HTTPException(409, f"Link cannot start a call ({link.state()})")
        await links.mark_call_started(token)
        log.info(
            "interview_call_requested", candidate_id=link.candidate_id, attempt=link.call_starts + 1
        )
    elif event.call_id:
        await links.set_call_id(token, event.call_id)
        log.info("interview_call_started", candidate_id=link.candidate_id, call_id=event.call_id)


def _message_page(request: Request, title: str, text: str, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "message.html", {"title": title, "text": text}, status_code=status_code
    )
