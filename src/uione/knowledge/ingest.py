"""Ingestion — filling the index, and keeping its permissions honest.

The index enforces permissions; this decides what they *are*. That is the harder
half, and it is where mirrored-permission designs meet their first real
disagreement with the source system.

Three rules, each the response to a specific way this goes wrong:

**An ACL that cannot be derived is not a document.** Not "index it and sort the
permissions out later" — later never arrives, and in the meantime the content is
either readable by everyone or invisible, and nobody knows which. A source that
cannot answer "who may read this?" contributes nothing.

**A source that fails mid-sync does not get partially applied.** A half-synced
source is a corpus where some documents have current permissions and others have
last week's, with no way to tell them apart.

**Re-sync is a deadline, not a background nicety.** A permission removed at the
source is a live leak until it lands here, so ACL refresh is separated from
content refresh: permissions can be re-verified often and cheaply without
refetching bodies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import structlog

from uione.knowledge.documents import AccessControl, Document
from uione.knowledge.index import DocumentIndex

log = structlog.get_logger(__name__)


class IngestionSource(Protocol):
    """Something that can produce documents and say who may read them."""

    @property
    def name(self) -> str: ...

    async def fetch(self, *, since: datetime | None = None) -> list[Document]: ...

    async def current_acls(self, document_ids: list[str]) -> dict[str, AccessControl] | None:
        """Permissions as they stand at the source, right now.

        Separate from :meth:`fetch` so revocation can be applied without
        refetching content — bodies are large and permissions change more often
        than text does.

        Returns ``None`` for a source whose permissions are static (a personal
        mailbox: the owner never changes). That is deliberately distinct from an
        empty dict, which means "the source knows about none of these documents"
        and therefore removes them. Conflating the two deletes a whole source on
        every refresh, quietly.
        """
        ...


@dataclass
class SyncResult:
    source: str
    indexed: int = 0
    skipped_no_acl: int = 0
    revoked: int = 0
    removed: int = 0
    failed: bool = False
    error: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if self.failed:
            return f"{self.source}: failed — {self.error}"
        parts = [f"{self.indexed} indexed"]
        if self.skipped_no_acl:
            parts.append(f"{self.skipped_no_acl} skipped (no ACL)")
        if self.revoked:
            parts.append(f"{self.revoked} permissions changed")
        if self.removed:
            parts.append(f"{self.removed} removed")
        return f"{self.source}: " + ", ".join(parts)


class Ingestor:
    def __init__(self, index: DocumentIndex) -> None:
        self._index = index
        self._sources: dict[str, IngestionSource] = {}
        self._last_sync: dict[str, datetime] = {}

    def register(self, source: IngestionSource) -> None:
        self._sources[source.name] = source

    @property
    def sources(self) -> list[str]:
        return list(self._sources)

    def last_sync(self, source: str) -> datetime | None:
        return self._last_sync.get(source)

    async def sync(self, source_name: str, *, incremental: bool = True) -> SyncResult:
        """Pull documents from one source into the index."""
        source = self._sources.get(source_name)
        if source is None:
            return SyncResult(source=source_name, failed=True, error="unknown source")

        since = self._last_sync.get(source_name) if incremental else None
        result = SyncResult(source=source_name)

        try:
            documents = await source.fetch(since=since)
        except Exception as exc:  # noqa: BLE001 — one dead source is not an outage
            log.warning("ingest.fetch_failed", source=source_name, error=str(exc))
            return SyncResult(source=source_name, failed=True, error=f"{type(exc).__name__}: {exc}")

        staged: list[Document] = []
        for document in documents:
            if document.acl.empty:
                # Refusing beats indexing-and-fixing-later, because later does
                # not arrive and in the meantime nobody knows what is exposed.
                result.skipped_no_acl += 1
                log.warning("ingest.no_acl", source=source_name, document=document.id)
                continue
            staged.append(document)

        # Applied only once the whole batch is understood: a half-synced source
        # mixes current and stale permissions with no way to tell them apart.
        for document in staged:
            self._index.add(document)
            result.indexed += 1

        self._last_sync[source_name] = datetime.now(UTC)
        log.info("ingest.synced", **{"source": source_name, "indexed": result.indexed})
        return result

    async def sync_all(self, *, incremental: bool = True) -> list[SyncResult]:
        results = []
        for name in self._sources:
            results.append(await self.sync(name, incremental=incremental))
        return results

    async def resync_permissions(self, source_name: str) -> SyncResult:
        """Re-verify permissions without refetching content.

        The operation with a deadline. Cheap on purpose so it can run often.
        """
        source = self._sources.get(source_name)
        if source is None:
            return SyncResult(source=source_name, failed=True, error="unknown source")

        known = self._index.document_ids_for_source(source_name)
        if not known:
            return SyncResult(source=source_name)

        result = SyncResult(source=source_name)
        try:
            current = await source.current_acls(known)
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest.acl_resync_failed", source=source_name, error=str(exc))
            return SyncResult(source=source_name, failed=True, error=f"{type(exc).__name__}: {exc}")

        if current is None:
            # Static permissions. Nothing to compare against, and treating that
            # as "the source knows about none of them" would delete everything.
            return result

        for document_id in known:
            acl = current.get(document_id)
            if acl is None:
                # The source no longer knows about it, or will not say who may
                # read it. Either way we no longer know, so it goes.
                self._index.remove(document_id)
                result.removed += 1
                continue
            existing = self._index.acl_of(document_id)
            if existing is not None and existing.fingerprint() != acl.fingerprint():
                self._index.update_acl(document_id, acl)
                result.revoked += 1

        if result.revoked or result.removed:
            log.info(
                "ingest.permissions_changed",
                source=source_name,
                changed=result.revoked,
                removed=result.removed,
            )
        return result

    async def resync_all_permissions(self) -> list[SyncResult]:
        return [await self.resync_permissions(name) for name in self._sources]

    def quarantine(self, source_name: str, *, reason: str) -> int:
        """Drop a source's content entirely.

        For when permissions cannot be verified at all. Removing the content is
        the correct response to not knowing who may see it — the alternative is
        serving documents under permissions of unknown age.
        """
        removed = self._index.remove_source(source_name)
        log.warning("ingest.source_quarantined", source=source_name, reason=reason, removed=removed)
        return removed


@dataclass
class CallableSource:
    """Adapts plain functions to :class:`IngestionSource`.

    Lets a connector contribute to the index without importing anything from the
    knowledge layer, which keeps the dependency pointing the right way.
    """

    name: str
    fetcher: Callable[[datetime | None], Awaitable[list[Document]]]
    acl_reader: Callable[[list[str]], Awaitable[dict[str, AccessControl]]] | None = None

    async def fetch(self, *, since: datetime | None = None) -> list[Document]:
        return await self.fetcher(since)

    async def current_acls(self, document_ids: list[str]) -> dict[str, AccessControl] | None:
        if self.acl_reader is None:
            # Static permissions — a personal mailbox, where the owner never
            # changes. None, not {}: an empty dict means "none of these exist
            # any more" and would delete the source's entire content.
            return None
        return await self.acl_reader(document_ids)
