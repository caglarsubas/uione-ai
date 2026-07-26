"""The scheduler, and the store of pre-generated briefs.

This is what makes the product's second claim — *it's proactive* — true. Until
now the brief only existed when someone asked for it, and asking cost eight
seconds of generation. A brief that is already waiting turns the signature moment
from "request and wait" into "it's there".

Two properties are treated as requirements rather than optimisations:

**Concurrency is bounded.** Generating briefs for a department at once would
saturate the model plane and make interactive chat unusable for everyone else.
Proactive work is background work and must yield to people who are waiting.

**One user's failure is contained.** A connector outage during Alice's brief must
not stop Bob's from being generated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from uione.mcphub import Principal
from uione.proactive.brief import Brief, BriefGenerator
from uione.proactive.schedule import JobKind, ScheduledJob

log = structlog.get_logger(__name__)


@dataclass
class StoredBrief:
    """A generated brief, waiting to be collected."""

    brief: Brief
    generated_at: datetime
    kind: JobKind = JobKind.MORNING_BRIEF

    def age(self, now: datetime) -> timedelta:
        return now - self.generated_at

    def is_fresh(self, now: datetime, max_age: timedelta) -> bool:
        return self.age(now) <= max_age


class BriefStore:
    """Latest brief per user.

    Only the most recent is kept. Yesterday's morning brief has no value — it
    describes a world that has moved on, and serving it would be worse than
    serving nothing.
    """

    def __init__(self) -> None:
        self._briefs: dict[str, StoredBrief] = {}

    def put(self, user_id: str, brief: Brief, *, kind: JobKind = JobKind.MORNING_BRIEF) -> None:
        self._briefs[user_id] = StoredBrief(brief=brief, generated_at=datetime.now(UTC), kind=kind)

    def get(
        self, user_id: str, *, now: datetime | None = None, max_age: timedelta | None = None
    ) -> StoredBrief | None:
        stored = self._briefs.get(user_id)
        if stored is None:
            return None
        if max_age is not None and not stored.is_fresh(now or datetime.now(UTC), max_age):
            return None
        return stored

    def drop(self, user_id: str) -> None:
        self._briefs.pop(user_id, None)

    def __len__(self) -> int:
        """Number of stored briefs.

        Note for callers: this makes an *empty* store falsy, so the common
        ``store or BriefStore()`` idiom silently discards a caller's empty store.
        Use an explicit ``is None`` check. (Learned the hard way — a test caught
        it, having quietly received a different store than it passed in.)
        """
        return len(self._briefs)


@dataclass
class SchedulerStats:
    ticks: int = 0
    generated: int = 0
    failed: int = 0
    skipped_not_due: int = 0


@dataclass
class Scheduler:
    """Runs due jobs, bounded and isolated."""

    generator: BriefGenerator
    store: BriefStore
    principal_for: Callable[[str], Principal]
    jobs: list[ScheduledJob] = field(default_factory=list)

    #: Concurrent generations. Small on purpose: proactive work must yield to
    #: people who are waiting on an interactive request.
    max_concurrency: int = 2

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    stats: SchedulerStats = field(default_factory=SchedulerStats)

    _task: asyncio.Task | None = None
    _stopping: asyncio.Event | None = None

    def add(self, job: ScheduledJob) -> ScheduledJob:
        self.jobs = [j for j in self.jobs if j.key != job.key]
        self.jobs.append(job)
        return job

    def for_user(self, user_id: str) -> list[ScheduledJob]:
        return [j for j in self.jobs if j.user_id == user_id]

    async def tick(self) -> int:
        """Run every job that is due. Returns how many were generated."""
        now = self.clock()
        self.stats.ticks += 1

        due = [job for job in self.jobs if job.is_due(now)]
        if not due:
            self.stats.skipped_not_due += len(self.jobs)
            return 0

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def run(job: ScheduledJob) -> bool:
            async with semaphore:
                return await self._run_job(job, now)

        results = await asyncio.gather(*(run(job) for job in due))
        return sum(results)

    async def _run_job(self, job: ScheduledJob, now: datetime) -> bool:
        principal = self.principal_for(job.user_id)
        # Marked before generating, so a job that raises does not spin on the
        # next tick and retry forever.
        job.last_run = now
        job.runs += 1
        try:
            brief = await self.generator.generate(principal, greeting=_greeting(job.kind))
        except Exception as exc:  # noqa: BLE001 — one user's failure is not everyone's
            job.failures += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            self.stats.failed += 1
            log.warning("scheduler.job_failed", user=job.user_id, error=job.last_error)
            return False

        job.last_error = None
        self.store.put(job.user_id, brief, kind=job.kind)
        self.stats.generated += 1
        log.info(
            "scheduler.brief_ready",
            user=job.user_id,
            kind=str(job.kind),
            complete=brief.complete,
        )
        return True

    # -- lifecycle ---------------------------------------------------------

    async def run_forever(self, *, interval_s: float = 60.0) -> None:
        """Tick until stopped."""
        self._stopping = asyncio.Event()
        log.info("scheduler.started", jobs=len(self.jobs), interval_s=interval_s)
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — the loop must outlive any one tick
                log.exception("scheduler.tick_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval_s)
            except TimeoutError:
                continue
        log.info("scheduler.stopped", **vars(self.stats))

    def start(self, *, interval_s: float = 60.0) -> asyncio.Task:
        self._task = asyncio.create_task(self.run_forever(interval_s=interval_s))
        return self._task

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None


def _greeting(kind: JobKind) -> str:
    return {
        JobKind.MORNING_BRIEF: "Good morning",
        JobKind.EVENING_SUMMARY: "Good evening",
        JobKind.WEEKLY_REVIEW: "Here is your week",
    }[kind]
