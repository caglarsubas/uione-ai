"""Engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from uione.config import Settings, get_settings
from uione.storage.models import Base

log = structlog.get_logger(__name__)


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: AsyncEngine = create_async_engine(
            self._settings.database_url,
            # SQLite writes are serialised anyway; a pool would only queue.
            echo=False,
            future=True,
        )
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        """Create tables if absent.

        Adequate while the schema is additive and pre-release. A migration tool
        becomes necessary the moment a customer has data worth preserving across
        a schema change — noted rather than pretended otherwise.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("storage.schema_ready", url=_safe_url(self._settings.database_url))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()


def _safe_url(url: str) -> str:
    """Strip credentials before logging a database URL."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _credentials, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
