"""The permission deadline, enforced.

`ingest.py` has always claimed that "re-sync is a deadline, not a background
nicety". Until this loop existed nothing enforced it: a permission revoked at the
source stayed live in our index for as long as the process lived.

The cases below are about the awkward half — what happens when permissions
*cannot* be verified. A source that answers is easy. A source that has been
silent for an hour is the one where a design either fails closed or quietly keeps
serving documents under permissions of unknown age.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uione.knowledge import (
    AccessControl,
    Document,
    DocumentIndex,
    IngestionRefresher,
    Ingestor,
)
from uione.mcphub import Principal

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"analyst"}))

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


class Clock:
    """A hand-wound clock, so a staleness budget can be tested in milliseconds."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


def document(doc_id: str, body: str, acl: AccessControl, source: str = "wiki") -> Document:
    return Document(id=doc_id, title=doc_id, body=body, source=source, acl=acl)


class FakeSource:
    """A source that can be told to fail, and to change its mind about ACLs."""

    def __init__(self, name: str = "wiki", documents: list[Document] | None = None) -> None:
        self.name = name
        self.documents = documents or [
            document("d1", "the payments runbook", AccessControl.organisation_wide())
        ]
        self.acls: dict[str, AccessControl] | None = None
        self.fail_fetch = False
        self.fail_acls = False
        self.fetches: list[datetime | None] = []

    async def fetch(self, *, since: datetime | None = None) -> list[Document]:
        if self.fail_fetch:
            raise ConnectionError("source unreachable")
        self.fetches.append(since)
        return list(self.documents)

    async def current_acls(self, document_ids: list[str]) -> dict[str, AccessControl] | None:
        if self.fail_acls:
            raise ConnectionError("source unreachable")
        if self.acls is None:
            return {d.id: d.acl for d in self.documents}
        return self.acls


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def index() -> DocumentIndex:
    return DocumentIndex()


@pytest.fixture
def refresher(index: DocumentIndex, source: FakeSource, clock: Clock) -> IngestionRefresher:
    ingestor = Ingestor(index)
    ingestor.register(source)
    return IngestionRefresher(
        ingestor=ingestor,
        clock=clock,
        max_acl_age_s=600,
    )


# -- the ordinary case -----------------------------------------------------


async def test_a_revocation_is_applied_by_the_permission_loop(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource
) -> None:
    """The point of the whole module."""
    await refresher.tick_content()
    assert index.search(BOB, "runbook"), "everyone can read it to begin with"

    source.acls = {"d1": AccessControl.for_users("alice")}
    await refresher.tick_permissions()

    assert index.search(ALICE, "runbook")
    assert index.search(BOB, "runbook") == [], "bob was removed at the source"


async def test_a_deletion_at_the_source_removes_the_document(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource
) -> None:
    await refresher.tick_content()

    source.acls = {}
    await refresher.tick_permissions()

    assert len(index) == 0


async def test_a_successful_check_records_its_age(
    refresher: IngestionRefresher, clock: Clock
) -> None:
    await refresher.tick_content()
    clock.advance(minutes=5)
    await refresher.tick_permissions()

    status = refresher.status()[0]

    assert status["acl_age_s"] == 0
    assert not status["quarantined"]


# -- the staleness budget --------------------------------------------------


async def test_a_brief_outage_does_not_drop_the_corpus(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource, clock: Clock
) -> None:
    """Failing closed on the first hiccup would make search useless."""
    await refresher.tick_content()
    source.fail_acls = True

    clock.advance(minutes=5)
    await refresher.tick_permissions()

    assert len(index) == 1
    assert not refresher.health["wiki"].quarantined
    assert refresher.health["wiki"].consecutive_failures == 1


async def test_a_long_outage_quarantines_the_source(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource, clock: Clock
) -> None:
    """Content served under permissions of unknown age is the thing being avoided.

    Search quietly getting worse is recoverable. A leak is not.
    """
    await refresher.tick_content()
    source.fail_acls = True

    clock.advance(minutes=5)
    await refresher.tick_permissions()
    clock.advance(minutes=20)  # past the 10-minute budget
    await refresher.tick_permissions()

    assert refresher.health["wiki"].quarantined
    assert len(index) == 0
    assert index.search(ALICE, "runbook") == []


async def test_the_quarantine_reason_is_recorded(
    refresher: IngestionRefresher, source: FakeSource, clock: Clock
) -> None:
    """An operator must be able to see why their corpus emptied."""
    await refresher.tick_content()
    source.fail_acls = True
    clock.advance(minutes=30)
    await refresher.tick_permissions()

    status = refresher.status()[0]

    assert status["quarantined"]
    assert "unverified" in status["reason"]
    assert status["acl_age_s"] >= 1800


async def test_a_source_is_quarantined_once_not_every_tick(
    refresher: IngestionRefresher, source: FakeSource, clock: Clock
) -> None:
    await refresher.tick_content()
    source.fail_acls = True
    clock.advance(minutes=30)
    await refresher.tick_permissions()
    await refresher.tick_permissions()

    assert refresher.stats.quarantines == 1


async def test_content_failures_do_not_start_the_quarantine_clock(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource, clock: Clock
) -> None:
    """Stale bodies are an inconvenience; stale permissions are a leak.

    Treating the two the same would drop a corpus over a slow wiki.
    """
    await refresher.tick_content()
    source.fail_fetch = True

    for _ in range(5):
        clock.advance(minutes=30)
        await refresher.tick_content()

    assert not refresher.health["wiki"].quarantined
    assert len(index) == 1


# -- recovery --------------------------------------------------------------


async def test_a_recovered_source_comes_back(
    refresher: IngestionRefresher, index: DocumentIndex, source: FakeSource, clock: Clock
) -> None:
    await refresher.tick_content()
    source.fail_acls = True
    clock.advance(minutes=30)
    await refresher.tick_permissions()
    assert len(index) == 0

    source.fail_acls = False
    clock.advance(minutes=5)
    await refresher.tick_permissions()

    assert not refresher.health["wiki"].quarantined
    assert index.search(ALICE, "runbook")


async def test_recovery_refetches_everything_not_just_recent_changes(
    refresher: IngestionRefresher, source: FakeSource, clock: Clock
) -> None:
    """The bug this prevents: a source that recovers but stays half-empty.

    An incremental fetch after a quarantine asks for changes since the last
    sync, so everything older never returns — and nothing looks wrong.
    """
    await refresher.tick_content()
    assert source.fetches == [None]

    source.fail_acls = True
    clock.advance(minutes=30)
    await refresher.tick_permissions()

    source.fail_acls = False
    await refresher.tick_permissions()

    assert source.fetches[-1] is None, "recovery must be a full fetch, not an incremental one"


async def test_a_recovered_source_reports_healthy(
    refresher: IngestionRefresher, source: FakeSource, clock: Clock
) -> None:
    await refresher.tick_content()
    source.fail_acls = True
    clock.advance(minutes=30)
    await refresher.tick_permissions()
    source.fail_acls = False
    await refresher.tick_permissions()

    status = refresher.status()[0]

    assert not status["quarantined"]
    assert status["reason"] == ""
    assert status["consecutive_failures"] == 0


# -- restart behaviour -----------------------------------------------------


async def test_stored_watermarks_seed_the_freshness_view(
    index: DocumentIndex, source: FakeSource, clock: Clock
) -> None:
    """A restart must not present a healthy estate as never-verified.

    With the budget applied naively that would quarantine everything on boot;
    without the seed it hides real staleness. Neither is acceptable, so the
    stored watermark is the starting point.
    """

    class StoredWatermarks:
        async def load_all(self):
            return {"wiki": NOW - timedelta(minutes=3)}

        async def save(self, source: str, when: datetime) -> None: ...

        async def delete(self, source: str) -> None: ...

    ingestor = Ingestor(index, watermarks=StoredWatermarks())
    ingestor.register(source)
    await ingestor.restore()

    refresher = IngestionRefresher(ingestor=ingestor, clock=clock, max_acl_age_s=600)

    assert refresher.status()[0]["acl_age_s"] == 180


async def test_an_estate_with_no_sources_starts_no_loops(index: DocumentIndex) -> None:
    """A deployment with nothing to index should not run two idle timers."""
    refresher = IngestionRefresher(ingestor=Ingestor(index))

    assert refresher.start() == []
    await refresher.stop()


# -- the loops actually run ------------------------------------------------


async def test_the_loops_run_and_stop_cleanly(index: DocumentIndex, source: FakeSource) -> None:
    """Started, ticked, stopped — with real asyncio rather than direct calls."""
    ingestor = Ingestor(index)
    ingestor.register(source)
    refresher = IngestionRefresher(ingestor=ingestor, content_interval_s=0.01, acl_interval_s=0.01)

    refresher.start()
    for _ in range(50):
        if refresher.stats.content_syncs and refresher.stats.acl_checks:
            break
        await __import__("asyncio").sleep(0.01)
    await refresher.stop()

    assert refresher.stats.content_syncs >= 1
    assert refresher.stats.acl_checks >= 1
    assert index.search(ALICE, "runbook")
