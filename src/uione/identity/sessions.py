"""Server-side sessions.

The browser holds an opaque session id in an httpOnly cookie; the access token
stays on the server. Two reasons, and the second is the one that matters:

* A token in a cookie readable by script is a token an XSS bug exfiltrates.
  httpOnly alone fixes that.
* **Logout can actually revoke.** A self-contained token in a cookie remains
  valid until it expires no matter what the user clicks, so "sign out" is a
  suggestion. Deleting a server-side row ends the session immediately, which is
  what an employee on a shared machine believes is happening.

Sessions live in the same database as the rest of the governance state, so they
survive a restart — otherwise every deployment silently signs everyone out.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select

from uione.mcphub import Principal
from uione.storage.database import Database
from uione.storage.models import SessionRow

log = structlog.get_logger(__name__)

COOKIE_NAME = "uione_session"

#: Sessions expire independently of the access token. A long-lived session with
#: a refresh token is a convenience decision; this is the ceiling on it.
DEFAULT_TTL = timedelta(hours=12)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


class SessionStore:
    def __init__(self, database: Database, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._db = database
        self._ttl = ttl

    async def create(
        self, principal: Principal, *, access_token: str = "", refresh_token: str = ""
    ) -> str:
        session_id = new_session_id()
        async with self._db.session() as session:
            session.add(
                SessionRow(
                    id=session_id,
                    principal_id=principal.user_id,
                    display_name=principal.display_name,
                    roles=sorted(principal.roles),
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=datetime.now(UTC) + self._ttl,
                )
            )
        log.info("identity.session_created", principal=principal.user_id)
        return session_id

    async def get(self, session_id: str) -> Principal | None:
        if not session_id:
            return None
        async with self._db.session() as session:
            row = await session.get(SessionRow, session_id)
            if row is None:
                return None
            # Compare in UTC. SQLite hands back naive datetimes, and a naive/aware
            # comparison raises rather than returning False — which would surface
            # as a 500 on every authenticated request.
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                await session.delete(row)
                return None
            return Principal(
                user_id=row.principal_id,
                roles=frozenset(row.roles or []),
                display_name=row.display_name or row.principal_id,
            )

    async def revoke(self, session_id: str) -> None:
        async with self._db.session() as session:
            if row := await session.get(SessionRow, session_id):
                await session.delete(row)
                log.info("identity.session_revoked", principal=row.principal_id)

    async def revoke_all_for(self, user_id: str) -> int:
        """Sign a user out everywhere. Needed when an account is compromised."""
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(SessionRow).where(SessionRow.principal_id == user_id)
                    )
                ).scalars()
            )
            for row in rows:
                await session.delete(row)
        return len(rows)

    async def purge_expired(self) -> int:
        async with self._db.session() as session:
            result = await session.execute(
                delete(SessionRow).where(SessionRow.expires_at <= datetime.now(UTC))
            )
            return result.rowcount or 0
