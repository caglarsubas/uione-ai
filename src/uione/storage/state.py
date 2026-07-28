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

from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy import func, select

from uione.a2a.contracts import ContractRegistry, DisclosureContract, Facet
from uione.analysis.anomaly import Point
from uione.knowledge.documents import AccessControl, Document, Visibility
from uione.knowledge.index import DocumentIndex
from uione.proactive.schedule import JobKind, Schedule, ScheduledJob
from uione.storage.database import Database
from uione.storage.models import (
    ConversationRow,
    DisclosureRow,
    DocumentRow,
    EmbeddingRow,
    McpPinRow,
    MetricPointRow,
    ScheduleRow,
    SyncWatermarkRow,
)

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


class McpPinStore:
    """The approved declaration of each MCP server.

    Returns ``None`` for a server never seen, which the pinning rules read as
    trust-on-first-use — distinct from ``{}``, a server approved with no tools.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def load(self, server: str) -> dict[str, str] | None:
        async with self._db.session() as session:
            row = await session.get(McpPinRow, server)
            return dict(row.tools or {}) if row is not None else None

    async def save(self, server: str, tools: dict[str, str], *, approved_by: str) -> None:
        async with self._db.session() as session:
            row = await session.get(McpPinRow, server)
            if row is None:
                row = McpPinRow(server=server)
                session.add(row)
            row.tools = dict(tools)
            row.approved_at = datetime.now(UTC)
            row.approved_by = approved_by

    async def load_all(self) -> dict[str, dict[str, str]]:
        async with self._db.session() as session:
            rows = list((await session.execute(select(McpPinRow))).scalars())
        return {row.server: dict(row.tools or {}) for row in rows}

    async def forget(self, server: str) -> bool:
        """Drop a pin, so the next start treats the server as new.

        How an operator approves a change: they look at what altered, then clear
        the pin so the current declaration becomes the approved one.
        """
        async with self._db.session() as session:
            row = await session.get(McpPinRow, server)
            if row is None:
                return False
            await session.delete(row)
            return True


class MetricStore:
    """The daily census, kept long enough to have an opinion about a week."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def record(self, principal_id: str, values: dict[str, float], *, at: datetime) -> int:
        """Write one day's numbers, replacing any already there for that day.

        Replacing rather than appending is what makes a retried scheduler tick
        harmless. Two rows for one Tuesday would quietly skew the detector's
        weekday baseline, and nothing would ever surface it.
        """
        day = at.astimezone(UTC).date().isoformat()
        async with self._db.session() as session:
            for metric, value in values.items():
                row = await session.get(MetricPointRow, (principal_id, metric, day))
                if row is None:
                    row = MetricPointRow(principal_id=principal_id, metric=metric, day=day)
                    session.add(row)
                row.value = float(value)
                row.at = at
        return len(values)

    async def history(self, principal_id: str, *, days: int = 60) -> dict[str, list[Point]]:
        """Every metric's series, oldest first.

        Bounded by `days` because the detector only ever looks back a few weeks,
        and loading a year to compute a Tuesday baseline is work nobody asked
        for.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(MetricPointRow)
                        .where(MetricPointRow.principal_id == principal_id)
                        .where(MetricPointRow.day >= cutoff)
                        .order_by(MetricPointRow.day)
                    )
                ).scalars()
            )

        series: dict[str, list[Point]] = {}
        for row in rows:
            when = datetime.fromisoformat(row.day).replace(tzinfo=UTC)
            series.setdefault(row.metric, []).append(Point(at=when, value=row.value))
        return series

    async def latest(self, principal_id: str) -> dict[str, float]:
        series = await self.history(principal_id, days=7)
        return {metric: points[-1].value for metric, points in series.items() if points}


class EmbeddingStore:
    """The vector cache. Expensive to produce, cheap to keep."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def save(self, document_id: str, vector: list[float], *, model: str, digest: str) -> None:
        async with self._db.session() as session:
            row = await session.get(EmbeddingRow, (document_id, model))
            if row is None:
                row = EmbeddingRow(document_id=document_id, model=model)
                session.add(row)
            row.content_hash = digest
            row.vector = list(vector)
            row.dims = len(vector)
            row.updated_at = datetime.now(UTC)

    async def load_into(self, vectors, *, model: str) -> int:
        """Restore vectors made by this exact model, and no other."""
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(select(EmbeddingRow).where(EmbeddingRow.model == model))
                ).scalars()
            )
        for row in rows:
            vectors.put(row.document_id, list(row.vector or []), digest=row.content_hash)
        return len(rows)

    async def delete(self, document_id: str) -> None:
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(EmbeddingRow).where(EmbeddingRow.document_id == document_id)
                    )
                ).scalars()
            )
            for row in rows:
                await session.delete(row)

    async def purge_other_models(self, model: str) -> int:
        """Drop vectors from models this deployment no longer uses.

        Called on startup rather than never: an operator who switches embedding
        model would otherwise carry the old corpus's vectors forever, invisible
        and consuming space nobody can account for.
        """
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(select(EmbeddingRow).where(EmbeddingRow.model != model))
                ).scalars()
            )
            for row in rows:
                await session.delete(row)
        if rows:
            log.info("storage.embeddings_purged", count=len(rows), keeping=model)
        return len(rows)


#: How much conversation to carry forward, in characters. A budget rather than a
#: message count because one tool result can be a whole document, and twenty
#: short turns cost less than two long ones.
HISTORY_BUDGET_CHARS = 12_000


class ConversationStore:
    """The running conversation, per person, server-side.

    Server-side is a security decision, not an architectural preference. History
    is model context: a client that supplies its own can fabricate an assistant
    turn — "the user already approved this" — and prime the model with it. That
    is the same class of attack the containment layer exists to stop, so nothing
    the browser says about what was said earlier is trusted.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def append(self, principal_id: str, messages: list, *, tainted: bool = False) -> int:
        """Add messages to the end of the conversation."""
        if not messages:
            return 0

        async with self._db.session() as session:
            highest = (
                await session.execute(
                    select(func.max(ConversationRow.seq)).where(
                        ConversationRow.principal_id == principal_id
                    )
                )
            ).scalar()
            seq = (highest or 0) + 1

            for message in messages:
                session.add(
                    ConversationRow(
                        principal_id=principal_id,
                        seq=seq,
                        role=message.role,
                        content=message.content,
                        tool_calls=[
                            {"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in (message.tool_calls or [])
                        ]
                        or None,
                        tool_call_id=message.tool_call_id,
                        name=message.name,
                        # Only tool output can bring untrusted content in, so the
                        # flag lands on the message that carried it.
                        tainted=tainted and message.role == "tool",
                    )
                )
                seq += 1
        return len(messages)

    async def history(
        self, principal_id: str, *, budget: int = HISTORY_BUDGET_CHARS
    ) -> tuple[list, bool]:
        """The recent conversation, and whether any of it is tainted.

        Returns messages oldest-first, trimmed from the front to fit the budget.

        **Trimming never orphans a tool result.** An OpenAI-compatible engine
        rejects a `tool` message whose assistant parent is missing, so a naive
        "keep the last N" produces a 400 the first time the cut lands mid-pair —
        which is exactly when a conversation has got long enough to matter.
        After trimming, leading `tool` messages are dropped for the same reason.
        """
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ConversationRow)
                        .where(ConversationRow.principal_id == principal_id)
                        .order_by(ConversationRow.seq)
                    )
                ).scalars()
            )

        kept: list = []
        spent = 0
        # Walk backwards so the most recent turns are the ones that survive.
        for row in reversed(rows):
            cost = len(row.content or "") + 64
            if spent + cost > budget and kept:
                break
            kept.append(row)
            spent += cost
        kept.reverse()

        # An assistant turn that requested tools must keep its results, and a
        # result cannot lead. Dropping from the front until the window starts on
        # something self-contained is what keeps the replay valid.
        while kept and kept[0].role == "tool":
            kept.pop(0)

        tainted = any(row.tainted for row in kept)
        return [_to_message(row) for row in kept], tainted

    async def clear(self, principal_id: str) -> int:
        """Start a new conversation. The audit log keeps what was said."""
        async with self._db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ConversationRow).where(ConversationRow.principal_id == principal_id)
                    )
                ).scalars()
            )
            for row in rows:
                await session.delete(row)
        log.info("storage.conversation_cleared", user=principal_id, messages=len(rows))
        return len(rows)


def _to_message(row: ConversationRow):
    from uione.modelplane.types import ChatMessage, ToolCall

    return ChatMessage(
        role=row.role,
        content=row.content,
        tool_calls=[
            ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
            for c in (row.tool_calls or [])
        ]
        or None,
        tool_call_id=row.tool_call_id,
        name=row.name,
    )
