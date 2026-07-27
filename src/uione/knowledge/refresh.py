"""Keeping the corpus — and its permissions — current.

`ingest.py` states that "re-sync is a deadline, not a background nicety", and
until now nothing enforced that deadline: ingestion ran when someone asked, or at
startup, and a permission revoked at the source stayed live in our index for as
long as the process lived. This is the loop that makes the claim true.

Two loops, not one, because the two operations have different costs and very
different urgency:

**Content sync** refetches bodies. Expensive, and being an hour behind on a wiki
page is an inconvenience.

**Permission re-sync** re-reads ACLs only. Cheap by design, and being an hour
behind means someone can read a document they were removed from an hour ago.
That is not an inconvenience, it is the failure this whole layer exists to
prevent — so it runs an order of magnitude more often.

**The staleness budget.** The harder question is what to do when permissions
*cannot* be verified — the source is down, credentials expired, the API changed.
Serving documents under permissions of unknown age is exactly the thing being
avoided, so after `max_acl_age` without a successful verification the source is
quarantined: its content is dropped from the index and from storage. That is
deliberately drastic. Search quietly getting worse is recoverable; a leak is not.

**Recovery refetches everything.** When a quarantined source comes back, its
watermark is cleared before re-syncing. An incremental fetch would ask for
changes since the last sync and return only recent ones, leaving the corpus
permanently missing everything older — a source that recovered but never came
back properly, with nothing to indicate why.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from uione.knowledge.ingest import Ingestor, SyncResult

log = structlog.get_logger(__name__)


@dataclass
class SourceHealth:
    """What is known about one source's freshness.

    Exposed rather than kept private: "how old are the permissions we are
    enforcing?" is a question an operator must be able to answer with a number,
    not an assumption.
    """

    source: str
    last_content_sync: datetime | None = None
    last_acl_check: datetime | None = None
    consecutive_failures: int = 0
    quarantined: bool = False
    reason: str = ""
    last_error: str = ""

    def acl_age(self, now: datetime) -> timedelta | None:
        if self.last_acl_check is None:
            return None
        return now - self.last_acl_check

    def is_stale(self, now: datetime, budget: timedelta) -> bool:
        """Whether permissions are older than we are willing to enforce.

        A source that has *never* been verified is not stale — it has not had
        its chance yet. The first verification failure starts the clock, which
        is what `last_acl_check` defaulting to the first sync accomplishes.
        """
        age = self.acl_age(now)
        return age is not None and age > budget

    def as_dict(self, now: datetime) -> dict:
        age = self.acl_age(now)
        return {
            "source": self.source,
            "last_content_sync": self.last_content_sync,
            "last_acl_check": self.last_acl_check,
            # `is not None`, not a truth test: a zero timedelta is falsy, so a
            # source verified this instant would report a null age — which reads
            # as "never verified", the opposite of the truth.
            "acl_age_s": int(age.total_seconds()) if age is not None else None,
            "consecutive_failures": self.consecutive_failures,
            "quarantined": self.quarantined,
            "reason": self.reason,
        }


@dataclass
class RefreshStats:
    content_syncs: int = 0
    acl_checks: int = 0
    failures: int = 0
    quarantines: int = 0
    recoveries: int = 0
    documents_removed: int = 0
    permissions_changed: int = 0


@dataclass
class IngestionRefresher:
    """Runs both sync loops and enforces the staleness budget."""

    ingestor: Ingestor

    #: Bodies. Slow on purpose — this is the expensive half.
    content_interval_s: float = 900.0

    #: Permissions. An order of magnitude more often, because this is the half
    #: with a security deadline.
    acl_interval_s: float = 120.0

    #: How old a successful permission check may be before the source is
    #: quarantined. Generous enough to survive a brief outage, short enough that
    #: a revocation cannot sit unapplied for a working day.
    max_acl_age_s: float = 3600.0

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    stats: RefreshStats = field(default_factory=RefreshStats)
    health: dict[str, SourceHealth] = field(default_factory=dict)

    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stopping: asyncio.Event | None = None

    def __post_init__(self) -> None:
        for name in self.ingestor.sources:
            self.health.setdefault(name, SourceHealth(source=name))
        # Seed from stored watermarks so a restart does not present every source
        # as never-synced — which would either hide real staleness or, with the
        # budget applied naively, quarantine a perfectly healthy estate on boot.
        for name, watermark in self.ingestor.watermarks.items():
            if name in self.health:
                self.health[name].last_content_sync = watermark
                self.health[name].last_acl_check = watermark

    @property
    def budget(self) -> timedelta:
        return timedelta(seconds=self.max_acl_age_s)

    def status(self) -> list[dict]:
        now = self.clock()
        return [h.as_dict(now) for h in self.health.values()]

    # -- the two operations -------------------------------------------------

    async def sync_content(self, source: str) -> SyncResult:
        health = self.health.setdefault(source, SourceHealth(source=source))
        result = await self.ingestor.sync(source)
        self.stats.content_syncs += 1

        if result.failed:
            # A content failure is noted but does not start the quarantine
            # clock: stale *bodies* are an inconvenience, and treating them like
            # stale permissions would drop a corpus over a slow wiki.
            health.last_error = result.error
            log.warning("refresh.content_failed", source=source, error=result.error)
            return result

        health.last_content_sync = self.clock()
        if health.last_acl_check is None:
            # A successful fetch means the source answered and its documents
            # carry ACLs, so permissions are current as of now.
            health.last_acl_check = health.last_content_sync
        return result

    async def check_permissions(self, source: str) -> SyncResult:
        """Re-verify one source's ACLs, quarantining if it has been too long."""
        health = self.health.setdefault(source, SourceHealth(source=source))
        now = self.clock()
        result = await self.ingestor.resync_permissions(source)
        self.stats.acl_checks += 1

        if result.failed:
            health.consecutive_failures += 1
            health.last_error = result.error
            self.stats.failures += 1
            log.warning(
                "refresh.acl_check_failed",
                source=source,
                error=result.error,
                consecutive=health.consecutive_failures,
                acl_age_s=(
                    int(age.total_seconds()) if (age := health.acl_age(now)) is not None else None
                ),
            )
            if health.is_stale(now, self.budget) and not health.quarantined:
                await self._quarantine(
                    source,
                    reason=f"permissions unverified for over {int(self.max_acl_age_s)}s",
                )
            return result

        health.consecutive_failures = 0
        health.last_acl_check = now
        health.last_error = ""
        self.stats.permissions_changed += result.revoked
        self.stats.documents_removed += result.removed

        if health.quarantined:
            await self._recover(source)

        return result

    async def _quarantine(self, source: str, *, reason: str) -> None:
        health = self.health[source]
        removed = await self.ingestor.quarantine(source, reason=reason)
        health.quarantined = True
        health.reason = reason
        self.stats.quarantines += 1
        self.stats.documents_removed += removed
        log.warning("refresh.quarantined", source=source, reason=reason, removed=removed)

    async def _recover(self, source: str) -> None:
        """Bring a quarantined source back, from scratch.

        The watermark is cleared first. Without that, the refetch is incremental
        against a watermark from before the quarantine and returns only what
        changed since — so the source comes back permanently missing everything
        older, looking healthy the whole time.
        """
        health = self.health[source]
        await self.ingestor.forget_watermark(source)
        result = await self.ingestor.sync(source, incremental=False)
        health.quarantined = False
        health.reason = ""
        self.stats.recoveries += 1
        log.info("refresh.recovered", source=source, indexed=result.indexed)

    # -- the loops ----------------------------------------------------------

    async def tick_content(self) -> list[SyncResult]:
        return [await self.sync_content(name) for name in self.ingestor.sources]

    async def tick_permissions(self) -> list[SyncResult]:
        return [await self.check_permissions(name) for name in self.ingestor.sources]

    async def _loop(self, name: str, operation, interval_s: float) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await operation()
            except Exception:  # noqa: BLE001 — the loop must outlive any one pass
                log.exception("refresh.tick_failed", loop=name)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval_s)
            except TimeoutError:
                continue

    def start(self) -> list[asyncio.Task]:
        if not self.ingestor.sources:
            log.info("refresh.no_sources")
            return []
        self._stopping = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._loop("content", self.tick_content, self.content_interval_s)),
            asyncio.create_task(
                self._loop("permissions", self.tick_permissions, self.acl_interval_s)
            ),
        ]
        log.info(
            "refresh.started",
            sources=self.ingestor.sources,
            content_interval_s=self.content_interval_s,
            acl_interval_s=self.acl_interval_s,
            max_acl_age_s=self.max_acl_age_s,
        )
        return self._tasks

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        for task in self._tasks:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._tasks = []
        if self.stats.content_syncs or self.stats.acl_checks:
            log.info("refresh.stopped", **vars(self.stats))
