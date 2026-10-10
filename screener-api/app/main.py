"""FastAPI entry point.

Phase 1: /health (liveness + DB check) and /auth/ping (tests the API key).
Later phases add /evaluate, /review/{thread_id} and /interview/{token}.
"""

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import psycopg
import structlog
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.logging_config import configure_logging
from app.security import require_api_key

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Code before `yield` runs once at startup, code after it at shutdown.
    Phase 2 will open the Postgres connection pool + LangGraph checkpointer here."""
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env == "prod")
    log.info("startup", version=__version__, env=settings.app_env)
    yield
    log.info("shutdown")


app = FastAPI(title="AI Internship Screener API", version=__version__, lifespan=lifespan)


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
