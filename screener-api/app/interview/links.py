"""Interview links: one random, unguessable token per invitation, stored in Postgres.

Why a DB-backed random token instead of putting candidate_id in the URL?
- Nobody can guess another candidate's link (token_urlsafe(24) = 192 bits of randomness).
- The browser can't be trusted (anyone can edit page JS / Vapi metadata), so /evaluate
  checks the token against this table to confirm which candidate a call belongs to.
- We record opened_at / call_started_at / evaluated_at, which n8n uses for edge cases:
  "never opened after 24h -> reminder", "started but no result -> incomplete".
"""

import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

MAX_CALL_STARTS = (
    2  # a link can start at most 2 calls (one retry after a dropped call) - caps Vapi cost
)


@dataclass(frozen=True)
class InterviewLink:
    token: str
    candidate_id: str
    candidate_name: str
    role: str
    created_at: datetime
    expires_at: datetime
    opened_at: datetime | None = None
    call_started_at: datetime | None = None
    call_starts: int = 0
    vapi_call_id: str | None = None
    evaluated_at: datetime | None = None
    thread_id: str | None = None

    def state(self, now: datetime | None = None) -> str:
        """Human-readable status, also used by n8n."""
        now = now or datetime.now(UTC)
        if self.evaluated_at:
            return "completed"
        if now >= self.expires_at:
            return "expired"
        if self.call_starts >= MAX_CALL_STARTS:
            return "attempts_used"
        if self.call_started_at:
            return "started"
        if self.opened_at:
            return "opened"
        return "sent"

    def can_start_call(self, now: datetime | None = None) -> bool:
        return self.state(now) in ("sent", "opened", "started")


def new_token() -> str:
    return secrets.token_urlsafe(24)


class LinkStore(Protocol):
    async def create(
        self, candidate_id: str, candidate_name: str, role: str, ttl: timedelta
    ) -> InterviewLink: ...
    async def get(self, token: str) -> InterviewLink | None: ...
    async def latest_for_candidate(self, candidate_id: str) -> InterviewLink | None: ...
    async def mark_opened(self, token: str) -> None: ...
    async def mark_call_started(self, token: str) -> None: ...
    async def set_call_id(self, token: str, vapi_call_id: str) -> None: ...
    async def mark_evaluated(self, token: str, thread_id: str) -> None: ...


# --------------------------------------------------------------------------- Postgres
SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS interview_links (
        token           TEXT PRIMARY KEY,
        candidate_id    TEXT NOT NULL,
        candidate_name  TEXT NOT NULL,
        role            TEXT NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at      TIMESTAMPTZ NOT NULL,
        opened_at       TIMESTAMPTZ,
        call_started_at TIMESTAMPTZ,
        call_starts     INTEGER NOT NULL DEFAULT 0,
        vapi_call_id    TEXT,
        evaluated_at    TIMESTAMPTZ,
        thread_id       TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS interview_links_candidate_idx ON interview_links (candidate_id, created_at DESC)",
]
COLUMNS = (
    "token, candidate_id, candidate_name, role, created_at, expires_at, opened_at, "
    "call_started_at, call_starts, vapi_call_id, evaluated_at, thread_id"
)


class PostgresLinkStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA_SQL:
                await conn.execute(statement)

    async def _one(self, sql: str, params: tuple) -> InterviewLink | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        return InterviewLink(**row) if row else None

    async def create(
        self, candidate_id: str, candidate_name: str, role: str, ttl: timedelta
    ) -> InterviewLink:
        link = await self._one(
            f"INSERT INTO interview_links (token, candidate_id, candidate_name, role, expires_at) "
            f"VALUES (%s, %s, %s, %s, now() + %s) RETURNING {COLUMNS}",
            (new_token(), candidate_id, candidate_name, role, ttl),
        )
        assert link is not None
        return link

    async def get(self, token: str) -> InterviewLink | None:
        return await self._one(f"SELECT {COLUMNS} FROM interview_links WHERE token = %s", (token,))

    async def latest_for_candidate(self, candidate_id: str) -> InterviewLink | None:
        return await self._one(
            f"SELECT {COLUMNS} FROM interview_links WHERE candidate_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (candidate_id,),
        )

    async def _exec(self, sql: str, params: tuple) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(sql, params)

    async def mark_opened(self, token: str) -> None:
        await self._exec(
            "UPDATE interview_links SET opened_at = COALESCE(opened_at, now()) WHERE token = %s",
            (token,),
        )

    async def mark_call_started(self, token: str) -> None:
        await self._exec(
            "UPDATE interview_links SET call_started_at = now(), call_starts = call_starts + 1 "
            "WHERE token = %s",
            (token,),
        )

    async def set_call_id(self, token: str, vapi_call_id: str) -> None:
        await self._exec(
            "UPDATE interview_links SET vapi_call_id = %s WHERE token = %s", (vapi_call_id, token)
        )

    async def mark_evaluated(self, token: str, thread_id: str) -> None:
        await self._exec(
            "UPDATE interview_links SET evaluated_at = now(), thread_id = %s WHERE token = %s",
            (thread_id, token),
        )


# --------------------------------------------------------------------------- in-memory (tests)
@dataclass
class InMemoryLinkStore:
    links: dict[str, InterviewLink] = field(default_factory=dict)

    async def create(
        self, candidate_id: str, candidate_name: str, role: str, ttl: timedelta
    ) -> InterviewLink:
        now = datetime.now(UTC)
        link = InterviewLink(new_token(), candidate_id, candidate_name, role, now, now + ttl)
        self.links[link.token] = link
        return link

    async def get(self, token: str) -> InterviewLink | None:
        return self.links.get(token)

    async def latest_for_candidate(self, candidate_id: str) -> InterviewLink | None:
        mine = [link for link in self.links.values() if link.candidate_id == candidate_id]
        return max(mine, key=lambda link: link.created_at) if mine else None

    async def mark_opened(self, token: str) -> None:
        link = self.links[token]
        self.links[token] = replace(link, opened_at=link.opened_at or datetime.now(UTC))

    async def mark_call_started(self, token: str) -> None:
        link = self.links[token]
        self.links[token] = replace(
            link, call_started_at=datetime.now(UTC), call_starts=link.call_starts + 1
        )

    async def set_call_id(self, token: str, vapi_call_id: str) -> None:
        self.links[token] = replace(self.links[token], vapi_call_id=vapi_call_id)

    async def mark_evaluated(self, token: str, thread_id: str) -> None:
        self.links[token] = replace(
            self.links[token], evaluated_at=datetime.now(UTC), thread_id=thread_id
        )
