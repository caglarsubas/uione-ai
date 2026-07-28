"""Engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from alembic.config import Config
from sqlalchemy import text
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
        """Create every table from the models, and record it as up to date.

        The fast path, for tests and for a first run on an empty database.
        Stamping matters: a database built this way is at the current revision,
        and without the stamp the next startup would report it as unmigrated and
        refuse to run.

        Production upgrades go through :meth:`upgrade` instead. `create_all`
        creates missing tables and never alters existing ones, so on a database
        that already has data it silently does nothing about a new column — and
        the failure arrives later as "no such column" in a query nobody changed.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._stamp)
        log.info("storage.schema_ready", url=_safe_url(self._settings.database_url))

    def _stamp(self, connection) -> None:
        from alembic.runtime.migration import MigrationContext

        context = MigrationContext.configure(connection)
        if context.get_current_revision() is None:
            context._ensure_version_table()
            connection.execute(_version_table().insert().values(version_num=head_revision()))

    async def backup_to(self, destination: str | Path) -> Path:
        """Write a consistent copy of the database, safely, while it is in use.

        **You cannot back up a live SQLite database by copying the file.** A
        `cp` taken mid-transaction captures pages from two different states and
        produces a file that opens, queries, and is wrong — the worst kind of
        backup, because nobody discovers it until they need it.

        `VACUUM INTO` is SQLite's own answer: it runs inside a read transaction
        and writes a complete, defragmented database. The result is smaller
        than the original and openable by any SQLite tool.

        Postgres is refused rather than half-supported. `pg_dump` already does
        this properly, with options this would have to reinvent badly, and an
        operator running Postgres has a backup policy that does not need us.
        """
        require_sqlite(self._settings.database_url)

        target = Path(destination).expanduser().resolve()
        if target.exists():
            # Refused rather than overwritten. Backups are taken on a schedule
            # and named by date; silently replacing one is how a week of them
            # turns out to be the same day.
            raise FileExistsError(f"{target} already exists; choose another name")
        target.parent.mkdir(parents=True, exist_ok=True)

        async with self._engine.connect() as conn:
            # Parameters are not allowed in VACUUM INTO, so the path is
            # embedded — with quotes doubled, which is SQLite's own escape.
            escaped = str(target).replace("'", "''")
            await conn.execute(text(f"VACUUM INTO '{escaped}'"))

        log.info("storage.backup_written", path=str(target), bytes=target.stat().st_size)
        return target

    async def has_tables(self) -> bool:
        """Whether this database holds any of our tables.

        The distinction that decides what startup does. An *empty* database is
        a first run and gets created. A *populated* database with no migration
        record predates migrations, and creating tables over it would fail on
        the first one — that case needs `stamp`, which is a decision for the
        operator rather than a guess made at boot.
        """

        def inspect(connection) -> bool:
            from sqlalchemy import inspect as sa_inspect

            existing = set(sa_inspect(connection).get_table_names())
            return bool(existing & set(Base.metadata.tables))

        async with self._engine.connect() as conn:
            return await conn.run_sync(inspect)

    async def stamp(self) -> str:
        """Record the database as current without running anything.

        For a deployment that predates migrations and whose schema already
        matches. On a database that is genuinely behind this marks it current
        while leaving it broken — which is why the CLI refuses to re-stamp.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(self._stamp)
        return head_revision()

    async def upgrade(self) -> str:
        """Run migrations to head. Returns the revision now in place."""
        async with self._engine.begin() as conn:
            await conn.run_sync(_upgrade_sync)
        current = await self.current_revision()
        log.info("storage.migrated", revision=current)
        return current or ""

    async def current_revision(self) -> str | None:
        """Which revision this database is at, or None if it has never run one."""

        def read(connection) -> str | None:
            from alembic.runtime.migration import MigrationContext

            return MigrationContext.configure(connection).get_current_revision()

        async with self._engine.connect() as conn:
            return await conn.run_sync(read)

    def knows_revision(self, revision: str | None) -> bool:
        """Whether this build has the migration a database claims to be at.

        False means the database is ahead of the code — someone rolled the
        application back. Worth distinguishing, because it is unrecoverable by
        migration and the fix is the opposite of the usual one.
        """
        if revision is None:
            return True
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(_alembic_config())
        try:
            return script.get_revision(revision) is not None
        except Exception:  # noqa: BLE001 — an unknown revision is the answer
            return False

    async def is_current(self) -> bool:
        return await self.current_revision() == head_revision()

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


def _alembic_config() -> Config:
    """Alembic pointed at the migrations that ship inside the package.

    Located relative to this module rather than to the working directory,
    because an installed product has no repository root and an operator running
    the upgrade is not standing in one.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def require_sqlite(url: str) -> None:
    """Refuse a backend this cannot back up, before anything else happens.

    Called before the engine is built, not after. Constructing the engine loads
    the driver, so on a Postgres deployment without asyncpg installed the
    operator would get `ModuleNotFoundError: asyncpg` — an error about a missing
    package, for an action that was never going to be supported.
    """
    if "sqlite" not in url:
        backend = url.split("://")[0] or "this backend"
        raise RuntimeError(
            f"backup handles SQLite only; this deployment uses {backend}. Use "
            "that engine's own tooling — pg_dump for PostgreSQL — which does "
            "the job properly and is already part of your backup policy."
        )


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head() or ""


def _version_table():
    from sqlalchemy import Column, MetaData, String, Table

    return Table("alembic_version", MetaData(), Column("version_num", String(32), nullable=False))


def _upgrade_sync(connection) -> None:
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    config = _alembic_config()
    script = ScriptDirectory.from_config(config)

    def revisions(rev, _context):
        return script._upgrade_revs("head", rev)

    with EnvironmentContext(config, script, fn=revisions, as_sql=False) as env:
        env.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with env.begin_transaction():
            env.run_migrations()


def _safe_url(url: str) -> str:
    """Strip credentials before logging a database URL."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _credentials, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
