"""Alembic environment.

Two things here are deliberate and neither is the default.

**The URL comes from the application's own settings.** Not from `alembic.ini`,
because the same image runs against SQLite on a laptop and Postgres in a
datacentre, and a connection string checked into a file is a connection string
in a git history. `UIONE_DATABASE_URL` drives both the app and its migrations,
so they cannot disagree about which database is being upgraded.

**Async drivers are used synchronously here.** The application talks to the
database over `aiosqlite` and `asyncpg`; Alembic's machinery is synchronous.
Rather than maintaining a second, sync URL for operators to keep in step — which
is a configuration mistake waiting to be made at 3am — the async driver is
driven through `run_sync`.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from uione.config import get_settings
from uione.storage.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    """Which database to migrate, most explicit first.

    ``-x url=...`` wins, so a migration can be pointed at a restored backup
    without exporting anything. A URL set on the config object comes next, which
    is how a caller inside the process — a test, or the `sql` command — chooses
    a target. Settings are the fallback, so the ordinary case needs no argument
    at all and cannot disagree with the application.
    """
    explicit = context.get_x_argument(as_dictionary=True).get("url")
    configured = config.get_main_option("sqlalchemy.url", "")
    return explicit or configured or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL rather than run it.

    The mode a change-controlled datacentre actually uses: a DBA wants the
    statements to review before anything touches production.
    """
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place, so Alembic rewrites the
        # table. Enabled globally rather than per-migration: a migration author
        # should not have to remember which backend the operator chose.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        # Type changes are compared too. Without this, widening a column is
        # silently not a migration, and the mismatch appears as a truncation
        # months later.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=NullPool)

    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
