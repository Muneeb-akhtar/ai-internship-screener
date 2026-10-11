"""FastAPI entry point.

Endpoints:
  GET  /health                 liveness + DB check (public)
  GET  /auth/ping              tests the X-API-Key header
  POST /evaluate               run the Screening Decision Agent on a transcript
  POST /review/{thread_id}     HR's APPROVE/REJECT for a borderline candidate (resumes the graph)
  GET  /evaluations/{thread_id} current result of an evaluation
  + interview routes (app/interview/routes.py): /interview-links, /interview/{token}, ...
"""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
import structlog
from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import __version__
from app.agent.graph import build_graph
from app.agent.nodes import ScreeningNodes
from app.agent.service import NotPendingReview, ScreeningService, ThreadNotFound
from app.audit import AuditLog
from app.config import get_settings
from app.interview.links import LinkStore, PostgresLinkStore
from app.interview.routes import get_links
from app.interview.routes import router as interview_router
from app.llm import build_llm
from app.logging_config import configure_logging
from app.schemas import EvaluateRequest, EvaluationResult, ReviewRequest
from app.security import require_api_key
from app.tools.calendar import build_scheduler

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs once at startup, code after it at shutdown."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env == "prod")
    log.info("startup", version=__version__, env=settings.app_env)

    # One shared connection pool for the checkpointer and the audit log.
    # autocommit + prepare_threshold=0 + dict_row are what AsyncPostgresSaver expects.
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=30)

    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()  # creates the checkpoint tables on first run (idempotent)
    audit = AuditLog(pool)
    await audit.setup()
    links = PostgresLinkStore(pool)
    await links.setup()
    app.state.links = links

    app.state.service = None
    try:
        llm = build_llm(settings)
        graph = build_graph(ScreeningNodes(llm, build_scheduler(settings)), checkpointer)
        app.state.service = ScreeningService(graph, audit, llm.model_name)
    except Exception as exc:  # e.g. missing API key: keep /health up, /evaluate returns 503
        log.error("agent_not_configured", error=str(exc))

    yield

    await pool.close()
    log.info("shutdown")


app = FastAPI(title="AI Internship Screener API", version=__version__, lifespan=lifespan)
app.include_router(interview_router)


@app.middleware("http")
async def request_logging(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Tag every request with an id, then log method/path/status/duration once it finishes."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled_error", method=request.method, path=request.url.path)
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error", "request_id": request_id}
        )
    response.headers["X-Request-ID"] = request_id
    # Skip the docker healthcheck noise (every 15s) unless it fails
    if request.url.path != "/health" or response.status_code != 200:
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    return response


# ----------------------------------------------------------------------- ops
async def check_database() -> str:
    """Open a short-lived connection and run SELECT 1. Returns "ok" or an error string."""
    try:
        async with await psycopg.AsyncConnection.connect(
            get_settings().database_url, connect_timeout=3
        ) as conn:
            await conn.execute("SELECT 1")
        return "ok"
    except Exception as exc:  # report any DB problem instead of crashing /health
        log.warning("health_db_failed", error=str(exc))
        return f"error: {type(exc).__name__}"


@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    """Used by Docker healthchecks, Caddy and uptime monitors. 503 if the DB is unreachable."""
    db = await check_database()
    healthy = db == "ok"
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "db": db, "version": __version__},
    )


@app.get("/auth/ping", tags=["ops"], dependencies=[Depends(require_api_key)])
async def auth_ping() -> dict[str, str]:
    """Returns 200 only with a valid X-API-Key header - handy for testing n8n credentials."""
    return {"status": "authenticated"}


# --------------------------------------------------------------------- agent
def get_service(request: Request) -> ScreeningService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(503, "Agent not configured (check LLM_PROVIDER / API key in .env)")
    return service


Service = Annotated[ScreeningService, Depends(get_service)]
ThreadId = Annotated[str, Path(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")]
protected = [Depends(require_api_key)]


@app.post("/evaluate", tags=["agent"], dependencies=protected)
async def evaluate(
    req: EvaluateRequest, service: Service, links: Annotated[LinkStore, Depends(get_links)]
) -> EvaluationResult:
    """Called by n8n after Vapi's end-of-call report. Synchronous: takes ~5-60 s (LLM calls)."""
    if req.interview_token:
        link = await links.get(req.interview_token)
        if link is None or link.candidate_id != req.candidate_id:
            log.warning("interview_token_mismatch", candidate_id=req.candidate_id)
            raise HTTPException(403, "interview_token does not belong to this candidate")
    try:
        result = await service.evaluate(req)
    except Exception as exc:
        # The checkpoint keeps the progress; n8n can retry with the same call_id to resume.
        log.exception("evaluation_failed", candidate_id=req.candidate_id)
        raise HTTPException(
            502, f"Evaluation failed: {type(exc).__name__}. Safe to retry."
        ) from exc
    # An incomplete call leaves the link usable for its remaining attempt
    if req.interview_token and result.decision != "incomplete":
        await links.mark_evaluated(req.interview_token, result.thread_id)
    return result


@app.post("/review/{thread_id}", tags=["agent"], dependencies=protected)
async def review(thread_id: ThreadId, req: ReviewRequest, service: Service) -> EvaluationResult:
    """HR's decision for a borderline candidate. Resumes the paused graph."""
    try:
        return await service.review(thread_id, req)
    except ThreadNotFound as exc:
        raise HTTPException(404, "Unknown thread_id") from exc
    except NotPendingReview as exc:
        raise HTTPException(409, "This evaluation is not waiting for HR review") from exc
    except Exception as exc:
        log.exception("review_failed", thread_id=thread_id)
        raise HTTPException(502, f"Review failed: {type(exc).__name__}. Safe to retry.") from exc


@app.get("/evaluations/{thread_id}", tags=["agent"], dependencies=protected)
async def get_evaluation(thread_id: ThreadId, service: Service) -> EvaluationResult:
    try:
        return await service.get(thread_id)
    except ThreadNotFound as exc:
        raise HTTPException(404, "Unknown thread_id") from exc
