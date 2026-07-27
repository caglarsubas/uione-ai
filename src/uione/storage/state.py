"""Durable stores for the state that was still in memory.

Each of these fails silently when lost, which is why they are worth persisting
even though nothing crashes without them:

* **Schedules** — nobody is told their morning brief stopped being prepared. They
  simply stop receiving one and conclude the feature does not work.
* **Disclosure contracts** — everyone reverts to the default, which is *narrower*
  than most will have configured, so colleagues' assistants quietly refuse
  questions they used to answer.
* **Documents** — search returns nothing until a re-ingest happens, which for a
  file share can be a long walk.
* **Sync watermarks** — the dangerous one. Without them an incremental sync after
  a restart either re-ingests everything, or worse, resumes from a watermark that
  never covered the window it skipped.

The inverted index is deliberately *not* stored. It is derived data: persisting
postings would force a schema change every time the tokeniser changes, and a
stale index disagreeing with its own documents is worse than one that takes a
second to rebuild.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import structlog
from sqlalchemy import select

from uione.a2a.contracts import ContractRegistry, DisclosureContract, Facet
from uione.knowledge.documents import AccessControl, Document, Visibility
from uione.knowledge.index import DocumentIndex
from uione.proactive.schedule import JobKind, Schedule, ScheduledJob
from uione.storage.database import Database
from uione.storage.models import DisclosureRow, DocumentRow, ScheduleRow, SyncWatermarkRow

log = structlog.get_logger(__name__)


def _parse_time(value: str) -> time:
    hour, _, minute = value.partition(":")
    try:
        return time(int(hour), int(minute or 0))
    except ValueError:
        log.warning("storage.bad_schedule_time", value=value)
        return time(7, 30)


class ScheduleStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, job: ScheduledJob) -> None:
        async with self._db.session() as session:
            row = await session.get(ScheduleRow, (job.user_id, str(job.kind)))
            if row is None:
                row = ScheduleRow(principal_id=job.user_id, kind=str(job.kind))
                session.add(row)
            row.at = job.schedule.at.strftime("%H:%M")
            row.timezone = job.schedule.timezone
            row.days = sorted(job.schedule.days)
            row.jitter_s = job.schedule.jitter_s
            row.enabled = job.enabled
            row.last_run = job.last_run
            row.runs = job.runs
            row.failures = job.failures

    async def load_all(self) -> list[ScheduledJob]:
        async with self._db.session() as session:
            rows = list((await session.execute(select(ScheduleRow))).scalars())

        jobs: list[ScheduledJob] = []
        for row in rows:
            job = ScheduledJob(
                user_id=row.principal_id,
                kind=JobKind(row.kind),
                schedule=Schedule(
                    at=_parse_time(row.at),
                    days=frozenset(row.days or []),
                    timezone=row.timezone,
                    jitter_s=row.jitter_s,
                ),
                enabled=row.enabled,
                runs=row.runs,
                failures=row.failures,
            )
            # Restored deliberately: without it every job looks brand new, and a
            # brand-new job is not due — so a restart after 08:00 would skip the
            # whole day, or, with different due-ness rules, fire for everyone at
            # once on boot.
            job.last_run = (
                row.last_run.replace(tzinfo=UTC)
                if row.last_run is not None and row.last_run.tzinfo is None
                else row.last_run
            )
            jobs.append(job)

        log.info("storage.schedules_loaded", count=len(jobs))
        return jobs

    async def delete(self, user_id: str, kind: JobKind) -> None:
        async with self._db.session() as session:
            if row := await session.get(ScheduleRow, (user_id, str(kind))):
                await session.delete(row)


class DisclosureStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, contract: DisclosureContract) -> None:
        async with self._db.session() as session:
            row = await session.get(DisclosureRow, contract.owner_id)
            if row is None:
                row = DisclosureRow(owner_id=contract.owner_id)
                session.add(row)
            row.default_facets = sorted(f.value for f in contract.default)
            row.external_facets = sorted(f.value for f in contract.external_default)
            row.by_role = {r: sorted(f.value for f in fs) for r, fs in contract.by_role.items()}
            row.by_user = {u: sorted(f.value for f in fs) for u, fs in contract.by_user.items()}
            row.updated_at = datetime.now(UTC)

    async def load_into(self, registry: ContractRegistry) -> int:
        async with self._db.session() as session:
            rows = list((await session.execute(select(DisclosureRow))).scalars())

        for row in rows:
            registry.set(
                DisclosureContract(
                    owner_id=row.owner_id,
                    default=_facets(row.default_facets),
                    external_default=_facets(row.external_facets),
                    by_role={r: _facets(f) for r, f in (row.by_role or {}).items()},
                    by_user={u: _facets(f) for u, f in (row.by_user or {}).items()},
                )
            )

        log.info("storage.contracts_loaded", count=len(rows))
        return len(rows)


def _facets(names: list[str] | None) -> frozenset[Facet]:
    """Parse stored facet names, dropping any this build no longer knows.

    An unknown facet is skipped rather than raising: a downgrade should narrow
    what is disclosed, not refuse to start.
    """
    resolved = set()
    for name in names or []:
        try:
            resolved.add(Facet(name))
        except ValueError:
            log.warning("storage.unknown_facet", facet=name)
    return frozenset(resolved)


class DocumentStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, document: Document) -> None:
        async with self._db.session() as session:
            row = await session.get(DocumentRow, document.id)
            if row is None:
                row = DocumentRow(id=document.id)
                session.add(row)
            row.title = document.title
            row.body = document.body
            row.source = document.source
            row.url = document.url
            row.acl_users = sorted(document.acl.users)
            row.acl_groups = sorted(document.acl.groups)
            row.acl_denied = sorted(document.acl.denied_users)
            row.acl_visibility = document.acl.visibility.value
            row.updated_at = document.updated_at
            row.doc_metadata = document.metadata

    async def save_all(self, documents: list[Document]) -> int:
        for document in documents:
            await self.save(document)
        return len(documents)

    async def update_acl(self, document_id: str, acl: AccessControl) -> None:
        """Write a permission change without touching content.

        The half of a re-sync with a deadline: a revocation at the source is a
        live leak until it lands, and re-reading bodies to apply one would make
        the cheap operation expensive enough to run rarely.
        """
        async with self._db.session() as session:
            if row := await session.get(DocumentRow, document_id):
                row.acl_users = sorted(acl.users)
                row.acl_groups = sorted(acl.groups)
                row.acl_denied = sorted(acl.denied_users)
                row.acl_visibility = acl.visibility.value

    async def delete(self, document_id: str) -> None:
        async with self._db.session() as session:
            if row := await session.get(DocumentRow, document_id):
                await session.delete(row)

    async def delete_source(self, source: str) -> int:
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(select(DocumentRow).where(DocumentRow.source == source))
                ).scalars()
            )
            for row in rows:
                await session.delete(row)
        return len(rows)

    async def load_into(self, index: DocumentIndex) -> int:
        """Rebuild the index from stored documents.

        Postings are recomputed rather than restored, because they are derived
        from the tokeniser and a stored index would silently disagree with its
        own documents the first time that changed.
        """
        async with self._db.session() as session:
            rows = list((await session.execute(select(DocumentRow))).scalars())

        for row in rows:
            index.add(
                Document(
                    id=row.id,
                    title=row.title,
                    body=row.body,
                    source=row.source,
                    url=row.url,
                    acl=AccessControl(
                        users=frozenset(row.acl_users or []),
                        groups=frozenset(row.acl_groups or []),
                        denied_users=frozenset(row.acl_denied or []),
                        visibility=_visibility(row.acl_visibility),
                    ),
                    updated_at=row.updated_at,
                    metadata=row.doc_metadata or {},
                )
            )

        log.info("storage.documents_loaded", count=len(rows))
        return len(rows)


def _visibility(value: str) -> Visibility:
    """Parse stored visibility, defaulting to the restrictive option.

    An unrecognised value must not become organisation-wide.
    """
    try:
        return Visibility(value)
    except ValueError:
        log.warning("storage.unknown_visibility", value=value)
        return Visibility.RESTRICTED


class WatermarkStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, source: str, when: datetime) -> None:
        async with self._db.session() as session:
            row = await session.get(SyncWatermarkRow, source)
            if row is None:
                row = SyncWatermarkRow(source=source)
                session.add(row)
            row.last_sync = when

    async def delete(self, source: str) -> None:
        async with self._db.session() as session:
            if row := await session.get(SyncWatermarkRow, source):
                await session.delete(row)

    async def load_all(self) -> dict[str, datetime]:
        async with self._db.session() as session:
            rows = list((await session.execute(select(SyncWatermarkRow))).scalars())
        return {
            row.source: (
                row.last_sync.replace(tzinfo=UTC) if row.last_sync.tzinfo is None else row.last_sync
            )
            for row in rows
        }
