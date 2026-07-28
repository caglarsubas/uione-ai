"""Migrations.

The test that matters most is `test_migrations_produce_the_same_schema_as_the_models`.
Everything else here checks behaviour; that one checks that the two descriptions
of the schema — the SQLAlchemy models and the migration history — have not
drifted apart. They drift silently, and the symptom appears months later on a
customer's database rather than in CI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from uione.config import Settings
from uione.storage import Database
from uione.storage.cli import main
from uione.storage.database import head_revision
from uione.storage.models import Base


def url_for(tmp_path, name: str = "m.db") -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


def sync_url(tmp_path, name: str = "m.db") -> str:
    return f"sqlite:///{tmp_path / name}"


@pytest.fixture
async def migrated(tmp_path):
    database = Database(Settings(database_url=url_for(tmp_path)))
    await database.upgrade()
    yield database
    await database.dispose()


# -- the drift check -------------------------------------------------------


def test_migrations_produce_the_same_schema_as_the_models(tmp_path) -> None:
    """The schema is described twice — by the models and by the migrations —
    and nothing but this test keeps the two the same.

    A column added to a model without a migration works in every test (which
    builds tables from the models) and is missing on every real deployment
    (which builds them from migrations). The failure surfaces as "no such
    column" in a query nobody changed.
    """
    from alembic import command

    from uione.storage.database import _alembic_config

    config = _alembic_config()
    # The async URL: env.py builds an async engine, because the application
    # does and keeping a second sync URL in step is a mistake waiting to happen.
    config.set_main_option("sqlalchemy.url", url_for(tmp_path, "from_migrations.db"))
    command.upgrade(config, "head")

    from_models = create_engine(sync_url(tmp_path, "from_models.db"))
    Base.metadata.create_all(from_models)

    migrated_engine = create_engine(sync_url(tmp_path, "from_migrations.db"))
    migrated_tables = {
        name: {c["name"] for c in inspect(migrated_engine).get_columns(name)}
        for name in inspect(migrated_engine).get_table_names()
        if name != "alembic_version"
    }
    model_tables = {
        name: {c["name"] for c in inspect(from_models).get_columns(name)}
        for name in inspect(from_models).get_table_names()
    }

    assert migrated_tables.keys() == model_tables.keys(), (
        "a table exists in one description of the schema and not the other"
    )
    for table, columns in model_tables.items():
        assert migrated_tables[table] == columns, f"columns differ on {table}"


# -- what upgrade does -----------------------------------------------------


async def test_an_empty_database_reaches_head(tmp_path) -> None:
    database = Database(Settings(database_url=url_for(tmp_path)))
    try:
        assert await database.current_revision() is None

        revision = await database.upgrade()

        assert revision == head_revision()
        assert await database.is_current()
    finally:
        await database.dispose()


async def test_upgrading_twice_is_harmless(migrated: Database) -> None:
    """Operators re-run things. So do orchestrators, on every restart."""
    before = await migrated.current_revision()

    await migrated.upgrade()

    assert await migrated.current_revision() == before


async def test_every_table_the_product_needs_exists_after_migrating(
    migrated: Database, tmp_path
) -> None:
    engine = create_engine(sync_url(tmp_path))
    try:
        present = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert set(Base.metadata.tables) <= present


async def test_data_survives_a_migration(tmp_path) -> None:
    """The whole point. An operator upgrading must not lose their audit trail."""
    from uione.mcphub import AuditLog, AuditOutcome, Principal, RiskClass
    from uione.storage import SqlAuditSink

    database = Database(Settings(database_url=url_for(tmp_path)))
    try:
        await database.upgrade()
        await AuditLog(SqlAuditSink(database)).record(
            principal=Principal(user_id="alice", roles=frozenset()),
            server="mail",
            tool="mail.send_reply",
            risk=RiskClass.EXTERNAL_FACING,
            outcome=AuditOutcome.ALLOWED,
            arguments={},
        )
    finally:
        await database.dispose()

    reopened = Database(Settings(database_url=url_for(tmp_path)))
    try:
        await reopened.upgrade()
        rows = await SqlAuditSink(reopened).recent()
    finally:
        await reopened.dispose()

    assert len(rows) == 1


# -- create_schema, the fast path ------------------------------------------


async def test_create_schema_leaves_the_database_at_head(tmp_path) -> None:
    """Without the stamp, a database built this way looks unmigrated and the
    next startup refuses to run against it."""
    database = Database(Settings(database_url=url_for(tmp_path)))
    try:
        await database.create_schema()

        assert await database.is_current()
    finally:
        await database.dispose()


async def test_an_empty_database_has_no_tables(tmp_path) -> None:
    database = Database(Settings(database_url=url_for(tmp_path)))
    try:
        assert not await database.has_tables()
    finally:
        await database.dispose()


async def test_a_populated_database_reports_its_tables(migrated: Database) -> None:
    """The distinction that decides what startup does: an empty database is a
    first run, a populated one with no migration record predates migrations."""
    assert await migrated.has_tables()


# -- startup ---------------------------------------------------------------


async def test_startup_creates_an_empty_database(tmp_path) -> None:
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path))
    database = Database(settings)
    try:
        await prepare_database(database, settings)

        assert await database.is_current()
    finally:
        await database.dispose()


async def test_startup_refuses_a_database_it_cannot_run_against(tmp_path) -> None:
    """Starting anyway means running new code against an old schema, which does
    not fail at startup where it would be noticed. It fails later, in one query,
    after the process has been serving traffic.
    """
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path))
    engine = create_engine(sync_url(tmp_path))
    Base.metadata.create_all(engine)
    engine.dispose()

    database = Database(settings)
    try:
        with pytest.raises(RuntimeError):
            await prepare_database(database, settings)
    finally:
        await database.dispose()


async def test_the_refusal_names_stamp_for_a_pre_migration_deployment(tmp_path) -> None:
    """A different failure from being behind, and it needs a different fix."""
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path))
    engine = create_engine(sync_url(tmp_path))
    Base.metadata.create_all(engine)
    engine.dispose()

    database = Database(settings)
    try:
        with pytest.raises(RuntimeError, match="stamp"):
            await prepare_database(database, settings)
    finally:
        await database.dispose()


async def test_a_database_ahead_of_the_code_says_so(tmp_path) -> None:
    """Somebody rolled the application back. Alembic's own error is a
    ResolutionError naming a hex string, which tells an operator nothing — and
    the fix here is the opposite of the usual one."""
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path))
    database = Database(settings)
    try:
        await database.upgrade()
        async with database.session() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = 'from_v9'"))

        with pytest.raises(RuntimeError, match="newer version"):
            await prepare_database(database, settings)
    finally:
        await database.dispose()


async def test_a_revision_this_build_does_not_have_is_recognised(tmp_path) -> None:
    database = Database(Settings(database_url=url_for(tmp_path)))
    try:
        assert database.knows_revision(head_revision())
        assert database.knows_revision(None), "an unmigrated database is not 'ahead'"
        assert not database.knows_revision("from_a_future_release")
    finally:
        await database.dispose()


async def test_auto_upgrade_is_not_consulted_before_the_schema_is_understood(
    tmp_path,
) -> None:
    """Even with auto-upgrade on, a database the build cannot understand stops
    startup rather than being migrated blindly."""
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path), db_auto_upgrade=True)
    database = Database(settings)
    try:
        await database.upgrade()
        async with database.session() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = 'from_v9'"))

        with pytest.raises(RuntimeError, match="newer version"):
            await prepare_database(database, settings)
    finally:
        await database.dispose()


async def test_a_pre_migration_database_needs_a_person_even_with_auto_upgrade(
    tmp_path,
) -> None:
    """Tables but no migration record. Auto-upgrade cannot help — it would try
    to create tables that already exist and fail on the first one."""
    from uione.api.deps import prepare_database

    settings = Settings(database_url=url_for(tmp_path), db_auto_upgrade=True)
    engine = create_engine(sync_url(tmp_path))
    Base.metadata.create_all(engine)
    engine.dispose()

    database = Database(settings)
    try:
        with pytest.raises(RuntimeError, match="predates migrations"):
            await prepare_database(database, settings)
    finally:
        await database.dispose()


# -- the operator CLI ------------------------------------------------------


def test_status_exits_nonzero_when_the_database_is_behind(tmp_path, monkeypatch) -> None:
    """So a deployment script can gate on it."""
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", url_for(tmp_path, "cli.db"))
    get_settings.cache_clear()

    assert main(["status"]) == 1


def test_upgrade_then_status_reports_current(tmp_path, monkeypatch, capsys) -> None:
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", url_for(tmp_path, "cli.db"))
    get_settings.cache_clear()

    assert main(["upgrade"]) == 0
    assert main(["status"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_stamp_refuses_to_overwrite_an_existing_revision(tmp_path, monkeypatch, capsys) -> None:
    """Stamping a database that is genuinely behind marks it current while
    leaving it broken, and the failure appears later somewhere else."""
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", url_for(tmp_path, "cli.db"))
    get_settings.cache_clear()
    main(["upgrade"])

    assert main(["stamp"]) == 1
    assert "refusing" in capsys.readouterr().out


def test_stamp_marks_a_pre_migration_database_without_running_anything(
    tmp_path, monkeypatch
) -> None:
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", url_for(tmp_path, "cli.db"))
    get_settings.cache_clear()
    engine = create_engine(sync_url(tmp_path, "cli.db"))
    Base.metadata.create_all(engine)
    engine.dispose()

    assert main(["stamp"]) == 0


def test_there_is_no_downgrade_command(capsys) -> None:
    """Alembic offers one and this deliberately does not expose it. The initial
    revision's downgrade drops the audit log, the approval history and the undo
    journal — a flag somebody eventually passes at 3am while fixing something
    else."""
    assert main(["downgrade"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_help_lists_what_is_available(capsys) -> None:
    main([])

    output = capsys.readouterr().out
    assert "status" in output and "upgrade" in output and "stamp" in output
    assert "downgrade" not in output


def test_sql_mode_prints_statements_without_touching_the_database(
    tmp_path, monkeypatch, capsys
) -> None:
    """The mode a change-controlled datacentre uses: a DBA reviews the SQL
    before anything runs."""
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", sync_url(tmp_path, "never.db"))
    get_settings.cache_clear()

    assert main(["sql"]) == 0
    assert "CREATE TABLE" in capsys.readouterr().out
    assert not (tmp_path / "never.db").exists()


# -- backup ----------------------------------------------------------------


async def test_a_backup_is_a_working_database(tmp_path, migrated: Database) -> None:
    """The point of `VACUUM INTO` over `cp`: what comes out opens and answers."""
    from uione.mcphub import AuditLog, AuditOutcome, Principal, RiskClass
    from uione.storage import SqlAuditSink

    await AuditLog(SqlAuditSink(migrated)).record(
        principal=Principal(user_id="alice", roles=frozenset()),
        server="mail",
        tool="mail.send_reply",
        risk=RiskClass.EXTERNAL_FACING,
        outcome=AuditOutcome.ALLOWED,
        arguments={},
    )

    await migrated.backup_to(tmp_path / "backup.db")

    restored = Database(Settings(database_url=url_for(tmp_path, "backup.db")))
    try:
        assert await restored.is_current(), "a backup must carry its schema version"
        assert len(await SqlAuditSink(restored).recent()) == 1
    finally:
        await restored.dispose()


async def test_a_backup_is_taken_while_the_database_is_in_use(tmp_path, migrated: Database) -> None:
    """`VACUUM INTO` runs inside a read transaction, which is why this is safe
    and `cp` is not — a copy taken mid-transaction captures pages from two
    states and produces a file that opens, queries, and is wrong."""
    from uione.mcphub import AuditLog, AuditOutcome, Principal, RiskClass
    from uione.storage import SqlAuditSink

    log = AuditLog(SqlAuditSink(migrated))
    for i in range(20):
        await log.record(
            principal=Principal(user_id="alice", roles=frozenset()),
            server="mail",
            tool="mail.search",
            risk=RiskClass.READ,
            outcome=AuditOutcome.ALLOWED,
            arguments={"n": i},
        )

    target = await migrated.backup_to(tmp_path / "hot.db")

    assert target.stat().st_size > 0
    # The live database keeps working afterwards.
    await log.record(
        principal=Principal(user_id="alice", roles=frozenset()),
        server="mail",
        tool="mail.search",
        risk=RiskClass.READ,
        outcome=AuditOutcome.ALLOWED,
        arguments={},
    )
    assert len(await SqlAuditSink(migrated).recent(limit=100)) == 21


async def test_a_backup_never_overwrites(tmp_path, migrated: Database) -> None:
    """Backups are taken on a schedule and named by date. Silently replacing
    one is how a week of them turns out to be the same day."""
    await migrated.backup_to(tmp_path / "b.db")

    with pytest.raises(FileExistsError):
        await migrated.backup_to(tmp_path / "b.db")


def test_a_backend_this_cannot_back_up_is_refused_by_name() -> None:
    """Before the engine is built, so the answer is not a missing-driver
    traceback about a thing that was never supported."""
    from uione.storage.database import require_sqlite

    require_sqlite("sqlite+aiosqlite:///x.db")

    with pytest.raises(RuntimeError, match="pg_dump"):
        require_sqlite("postgresql+asyncpg://u@h/db")


async def test_a_restored_backup_from_an_older_build_still_refuses_to_run(
    tmp_path, migrated: Database
) -> None:
    """The interlock worth having: restoring last month's backup onto this
    month's code is caught by the same check that catches any stale schema,
    rather than by a query failing in production."""
    from uione.api.deps import prepare_database

    await migrated.backup_to(tmp_path / "old.db")
    settings = Settings(database_url=url_for(tmp_path, "old.db"))
    restored = Database(settings)
    try:
        async with restored.session() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = 'from_v9'"))

        with pytest.raises(RuntimeError, match="newer version"):
            await prepare_database(restored, settings)
    finally:
        await restored.dispose()
