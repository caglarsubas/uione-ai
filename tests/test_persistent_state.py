"""The last in-memory state, made durable.

Same assertion shape as `test_storage.py`: write, throw the store away, build a
new one over the same file, and find the state still there. A store that keeps
everything in a Python dict passes every other kind of test.

What makes this set different from approvals and audit is that none of these
failures announce themselves. A lost schedule produces no error — the brief just
stops arriving. A lost disclosure contract produces no error — a colleague's
assistant just starts refusing. So each case below asserts on the *consequence*
(is the job still due at the right time, is the facet still granted, can the user
still find the document) rather than on rows being present.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from uione.a2a import ContractRegistry, DisclosureContract, Facet
from uione.config import Settings
from uione.knowledge import AccessControl, Document, DocumentIndex, Ingestor, Visibility
from uione.mcphub import Principal
from uione.proactive import JobKind, Schedule, ScheduledJob
from uione.storage import (
    Database,
    DisclosureStore,
    DocumentStore,
    ScheduleStore,
    WatermarkStore,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"engineering"}))


@pytest.fixture
async def db(tmp_path):
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}"))
    await database.create_schema()
    yield database
    await database.dispose()


def reopen(tmp_path) -> Database:
    """A fresh Database over the same file — the restart being simulated."""
    return Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}"))


async def after_restart(tmp_path, work):
    """Run `work` against a database built from scratch over the same file."""
    second = reopen(tmp_path)
    try:
        return await work(second)
    finally:
        await second.dispose()


# -- schedules -------------------------------------------------------------


async def test_a_schedule_survives_a_restart(db: Database, tmp_path) -> None:
    await ScheduleStore(db).save(
        ScheduledJob(
            user_id="alice",
            schedule=Schedule(at=time(6, 45), timezone="Europe/Istanbul", jitter_s=300),
        )
    )

    jobs = await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all())

    assert len(jobs) == 1
    assert jobs[0].user_id == "alice"
    assert jobs[0].schedule.at == time(6, 45)
    assert jobs[0].schedule.timezone == "Europe/Istanbul"
    assert jobs[0].schedule.jitter_s == 300


async def test_the_last_run_survives_so_a_restart_does_not_refire(db: Database, tmp_path) -> None:
    """The reason `last_run` is stored at all.

    Without it every restored job looks brand new. Depending on which way the
    due-ness rule falls that means either the whole fleet generating at once on
    boot, or nobody's brief until tomorrow — and a restart at 09:00 happens.
    """
    ran_at = datetime(2026, 7, 27, 7, 31, tzinfo=UTC)
    job = ScheduledJob(user_id="alice", schedule=Schedule(at=time(7, 30), jitter_s=0))
    job.last_run = ran_at
    job.runs = 12
    await ScheduleStore(db).save(job)

    restored = (await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all()))[0]

    assert restored.last_run == ran_at
    assert restored.runs == 12
    # Ten minutes after it last ran, it is not due again.
    assert not restored.is_due(datetime(2026, 7, 27, 7, 41, tzinfo=UTC))
    # Tomorrow morning, it is.
    assert restored.is_due(datetime(2026, 7, 28, 7, 35, tzinfo=UTC))


async def test_a_disabled_schedule_stays_disabled(db: Database, tmp_path) -> None:
    """A user who switched their brief off must not find it back on."""
    await ScheduleStore(db).save(ScheduledJob(user_id="alice", enabled=False))

    restored = (await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all()))[0]

    assert not restored.enabled


async def test_saving_the_same_job_twice_updates_it(db: Database, tmp_path) -> None:
    store = ScheduleStore(db)
    await store.save(ScheduledJob(user_id="alice", schedule=Schedule(at=time(7, 0))))
    await store.save(ScheduledJob(user_id="alice", schedule=Schedule(at=time(9, 0))))

    jobs = await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all())

    assert len(jobs) == 1, "a user has one morning brief, not one per edit"
    assert jobs[0].schedule.at == time(9, 0)


async def test_jobs_of_different_kinds_coexist(db: Database, tmp_path) -> None:
    store = ScheduleStore(db)
    await store.save(ScheduledJob(user_id="alice", kind=JobKind.MORNING_BRIEF))
    await store.save(ScheduledJob(user_id="alice", kind=JobKind.WEEKLY_REVIEW))

    jobs = await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all())

    assert {str(j.kind) for j in jobs} == {"morning_brief", "weekly_review"}


async def test_a_deleted_schedule_stays_deleted(db: Database, tmp_path) -> None:
    store = ScheduleStore(db)
    await store.save(ScheduledJob(user_id="alice"))
    await store.delete("alice", JobKind.MORNING_BRIEF)

    assert await after_restart(tmp_path, lambda d: ScheduleStore(d).load_all()) == []


async def test_a_corrupt_stored_time_does_not_stop_startup(db: Database, tmp_path) -> None:
    """A hand-edited row must not prevent everyone else's brief from loading."""
    async with db.session() as session:
        from uione.storage.models import ScheduleRow

        session.add(ScheduleRow(principal_id="alice", kind="morning_brief", at="not-a-time"))

    jobs = await ScheduleStore(db).load_all()

    assert len(jobs) == 1
    assert jobs[0].schedule.at == time(7, 30)


# -- disclosure contracts --------------------------------------------------


async def test_a_disclosure_contract_survives_a_restart(db: Database, tmp_path) -> None:
    contract = DisclosureContract(owner_id="bob")
    contract.grant(role="engineering", facets=frozenset({Facet.TASK_DETAIL}))
    contract.grant(user="alice", facets=frozenset({Facet.MEETING_SUBJECTS}))
    await DisclosureStore(db).save(contract)

    async def load(database: Database) -> ContractRegistry:
        registry = ContractRegistry()
        await DisclosureStore(database).load_into(registry)
        return registry

    registry = await after_restart(tmp_path, load)
    restored = registry.for_owner("bob")

    assert restored.by_role["engineering"] == frozenset({Facet.TASK_DETAIL})
    assert restored.by_user["alice"] == frozenset({Facet.MEETING_SUBJECTS})


async def test_a_widened_contract_still_grants_after_a_restart(db: Database, tmp_path) -> None:
    """The consequence, not the row.

    Losing this reverts Bob to the default, which is *narrower* — so his
    assistant starts refusing a question it answered yesterday, with nothing in
    any log connecting the two events.
    """
    contract = DisclosureContract(owner_id="bob")
    contract.grant(user="alice", facets=frozenset({Facet.TASK_DETAIL, Facet.WORKLOAD}))
    await DisclosureStore(db).save(contract)

    async def evaluate(database: Database):
        registry = ContractRegistry()
        await DisclosureStore(database).load_into(registry)
        return registry.evaluate(
            owner_id="bob",
            requester_id="alice",
            requester_roles=frozenset({"analyst"}),
            requested=frozenset({Facet.TASK_DETAIL}),
        )

    disclosure = await after_restart(tmp_path, evaluate)

    assert Facet.TASK_DETAIL in disclosure.granted
    assert not disclosure.withheld


async def test_a_narrowed_contract_still_withholds_after_a_restart(db: Database, tmp_path) -> None:
    contract = DisclosureContract(owner_id="bob", default=frozenset({Facet.FREE_BUSY}))
    await DisclosureStore(db).save(contract)

    async def evaluate(database: Database):
        registry = ContractRegistry()
        await DisclosureStore(database).load_into(registry)
        return registry.evaluate(
            owner_id="bob",
            requester_id="alice",
            requester_roles=frozenset({"analyst"}),
            requested=frozenset({Facet.MEETING_SUBJECTS}),
        )

    disclosure = await after_restart(tmp_path, evaluate)

    assert Facet.MEETING_SUBJECTS in disclosure.withheld


async def test_an_unknown_facet_is_dropped_not_fatal(db: Database, tmp_path) -> None:
    """A downgrade should narrow what is disclosed, not refuse to start."""
    async with db.session() as session:
        from uione.storage.models import DisclosureRow

        session.add(
            DisclosureRow(owner_id="bob", default_facets=["free_busy", "telepathy"]),
        )

    registry = ContractRegistry()
    await DisclosureStore(db).load_into(registry)

    assert registry.for_owner("bob").default == frozenset({Facet.FREE_BUSY})


# -- documents -------------------------------------------------------------


def document(doc_id: str, body: str, acl: AccessControl, source: str = "wiki") -> Document:
    return Document(id=doc_id, title=doc_id, body=body, source=source, acl=acl)


async def test_a_document_is_searchable_after_a_restart(db: Database, tmp_path) -> None:
    await DocumentStore(db).save(
        document("d1", "the quarterly payments runbook", AccessControl.for_users("alice"))
    )

    async def rebuild(database: Database) -> DocumentIndex:
        index = DocumentIndex()
        await DocumentStore(database).load_into(index)
        return index

    index = await after_restart(tmp_path, rebuild)

    hits = index.search(ALICE, "runbook")
    assert [h.document.id for h in hits] == ["d1"]


async def test_permissions_survive_the_rebuild(db: Database, tmp_path) -> None:
    """The one that matters. A restored corpus with lost ACLs is a leak."""
    store = DocumentStore(db)
    await store.save(document("open", "shared runbook", AccessControl.organisation_wide()))
    await store.save(document("secret", "secret runbook", AccessControl.for_users("alice")))
    await store.save(
        document(
            "denied",
            "denied runbook",
            AccessControl(visibility=Visibility.ORGANISATION, denied_users=frozenset({"bob"})),
        )
    )

    async def rebuild(database: Database) -> DocumentIndex:
        index = DocumentIndex()
        await DocumentStore(database).load_into(index)
        return index

    index = await after_restart(tmp_path, rebuild)

    assert {h.document.id for h in index.search(ALICE, "runbook")} == {
        "open",
        "secret",
        "denied",
    }
    assert {h.document.id for h in index.search(BOB, "runbook")} == {"open"}


async def test_a_revoked_permission_is_written_through(db: Database, tmp_path) -> None:
    """A revocation that only lives in memory comes back on the next restart."""
    store = DocumentStore(db)
    await store.save(document("d1", "the payments runbook", AccessControl.organisation_wide()))
    await store.update_acl("d1", AccessControl.for_users("alice"))

    async def rebuild(database: Database) -> DocumentIndex:
        index = DocumentIndex()
        await DocumentStore(database).load_into(index)
        return index

    index = await after_restart(tmp_path, rebuild)

    assert index.search(ALICE, "runbook")
    assert index.search(BOB, "runbook") == []


async def test_a_deleted_document_does_not_come_back(db: Database, tmp_path) -> None:
    store = DocumentStore(db)
    await store.save(document("d1", "runbook", AccessControl.organisation_wide()))
    await store.delete("d1")

    async def rebuild(database: Database) -> DocumentIndex:
        index = DocumentIndex()
        await DocumentStore(database).load_into(index)
        return index

    assert len(await after_restart(tmp_path, rebuild)) == 0


async def test_a_quarantined_source_does_not_come_back(db: Database, tmp_path) -> None:
    """Quarantine means we could not verify who may read it.

    Restoring that content on the next restart would serve documents under
    permissions of unknown age, which is the exact thing quarantine exists to
    prevent.
    """
    store = DocumentStore(db)
    await store.save(document("w1", "runbook", AccessControl.organisation_wide(), "wiki"))
    await store.save(document("m1", "note", AccessControl.for_users("alice"), "mail"))

    index = DocumentIndex()
    ingestor = Ingestor(index, documents=store)
    await ingestor.restore()
    await ingestor.quarantine("wiki", reason="permission sync broken")

    async def rebuild(database: Database) -> DocumentIndex:
        rebuilt = DocumentIndex()
        await DocumentStore(database).load_into(rebuilt)
        return rebuilt

    restored = await after_restart(tmp_path, rebuild)

    assert restored.search(ALICE, "runbook") == []
    assert restored.search(ALICE, "note")


async def test_an_unknown_visibility_falls_back_to_restricted(db: Database, tmp_path) -> None:
    """An unrecognised stored value must not become organisation-wide."""
    async with db.session() as session:
        from uione.storage.models import DocumentRow

        session.add(
            DocumentRow(
                id="d1",
                title="runbook",
                body="the payments runbook",
                source="wiki",
                acl_visibility="everyone-obviously",
            )
        )

    index = DocumentIndex()
    await DocumentStore(db).load_into(index)

    assert index.search(ALICE, "runbook") == []


# -- ingestion end to end --------------------------------------------------


class StaticSource:
    def __init__(self, name: str, documents: list[Document]) -> None:
        self.name = name
        self._documents = documents
        self.fetches = 0
        self.since: datetime | None = None

    async def fetch(self, *, since: datetime | None = None) -> list[Document]:
        self.fetches += 1
        self.since = since
        return self._documents

    async def current_acls(self, document_ids: list[str]) -> dict[str, AccessControl] | None:
        return None


async def test_ingested_documents_are_searchable_after_a_restart(db: Database, tmp_path) -> None:
    """The whole path: sync once, restart, search without syncing again."""
    ingestor = Ingestor(DocumentIndex(), watermarks=WatermarkStore(db), documents=DocumentStore(db))
    ingestor.register(
        StaticSource(
            "wiki", [document("d1", "the payments runbook", AccessControl.for_users("alice"))]
        )
    )
    await ingestor.sync("wiki")

    async def rebuild(database: Database) -> DocumentIndex:
        index = DocumentIndex()
        restored = Ingestor(
            index, watermarks=WatermarkStore(database), documents=DocumentStore(database)
        )
        await restored.restore()
        return index

    index = await after_restart(tmp_path, rebuild)

    assert [h.document.id for h in index.search(ALICE, "runbook")] == ["d1"]


async def test_the_watermark_survives_so_the_next_sync_is_incremental(
    db: Database, tmp_path
) -> None:
    """Without this a restart either re-ingests everything or skips a window.

    The second is the dangerous one: a source asked for changes "since now"
    never returns what changed while the service was down.
    """
    ingestor = Ingestor(DocumentIndex(), watermarks=WatermarkStore(db), documents=DocumentStore(db))
    ingestor.register(StaticSource("wiki", [document("d1", "text", AccessControl.for_users("a"))]))
    await ingestor.sync("wiki")
    first_watermark = ingestor.last_sync("wiki")

    async def resync(database: Database) -> StaticSource:
        index = DocumentIndex()
        restored = Ingestor(
            index, watermarks=WatermarkStore(database), documents=DocumentStore(database)
        )
        source = StaticSource("wiki", [document("d2", "more", AccessControl.for_users("a"))])
        restored.register(source)
        await restored.restore()
        await restored.sync("wiki")
        return source

    source = await after_restart(tmp_path, resync)

    assert source.since is not None, "a restart must not silently refetch everything"
    assert source.since == first_watermark


# -- through the API, across an actual restart -----------------------------


@pytest.fixture
def app_factory(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Start the whole app over one database file, as many times as needed.

    The unit tests above prove each store round-trips. This proves the *wiring*
    does — which is where this kind of change actually fails: a store that works
    perfectly but is never called on the write path, or never loaded on the read
    path, produces exactly the same symptom as no persistence at all.
    """
    from fastapi.testclient import TestClient

    from uione.api.app import create_app
    from uione.config import get_settings

    monkeypatch.setenv("UIONE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    # Off, or a tick during the test would generate briefs against a stub model.
    monkeypatch.setenv("UIONE_SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    def start() -> TestClient:
        return TestClient(create_app())

    yield start
    get_settings.cache_clear()


HEADERS = {"X-User-Id": "alice", "X-User-Roles": "analyst"}


def test_a_schedule_set_through_the_api_survives_a_restart(app_factory) -> None:
    with app_factory() as client:
        r = client.put(
            "/me/schedule", json={"at": "06:15", "timezone": "Europe/Istanbul"}, headers=HEADERS
        )
        assert r.status_code == 200

    with app_factory() as restarted:
        schedules = restarted.get("/me/schedule", headers=HEADERS).json()

    assert len(schedules) == 1
    assert schedules[0]["at"] == "06:15"
    assert schedules[0]["timezone"] == "Europe/Istanbul"


def test_a_disclosure_change_through_the_api_survives_a_restart(app_factory) -> None:
    with app_factory() as client:
        r = client.put(
            "/me/disclosure",
            json={"by_user": {"bob": ["task_detail", "workload"]}},
            headers=HEADERS,
        )
        assert r.status_code == 200

    with app_factory() as restarted:
        contract = restarted.get("/me/disclosure", headers=HEADERS).json()

    assert contract["by_user"]["bob"] == ["task_detail", "workload"]


def test_only_the_owners_schedule_comes_back_to_them(app_factory) -> None:
    """Restored state stays attached to the person it belongs to."""
    with app_factory() as client:
        client.put("/me/schedule", json={"at": "06:15"}, headers=HEADERS)
        client.put(
            "/me/schedule",
            json={"at": "09:45"},
            headers={"X-User-Id": "bob", "X-User-Roles": "analyst"},
        )

    with app_factory() as restarted:
        alice = restarted.get("/me/schedule", headers=HEADERS).json()
        bob = restarted.get(
            "/me/schedule", headers={"X-User-Id": "bob", "X-User-Roles": "analyst"}
        ).json()

    assert [s["at"] for s in alice] == ["06:15"]
    assert [s["at"] for s in bob] == ["09:45"]
