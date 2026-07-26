from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from uione.mcphub import Principal
from uione.proactive import (
    WEEKDAYS,
    Brief,
    BriefStore,
    JobKind,
    Schedule,
    ScheduledJob,
    Scheduler,
)

MONDAY_6AM = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)
FRIDAY_9AM = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
SATURDAY_9AM = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def principal_for(user_id: str) -> Principal:
    return Principal(user_id=user_id, roles=frozenset({"analyst"}))


def a_brief(user_id: str = "alice") -> Brief:
    return Brief(principal_id=user_id, generated_at=datetime.now(UTC), body="Your brief.")


class StubGenerator:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_for = fail_for or set()
        self.concurrent = 0
        self.max_concurrent = 0

    async def generate(self, principal: Principal, *, greeting: str = "", **kwargs) -> Brief:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.calls.append((principal.user_id, greeting))
            if principal.user_id in self.fail_for:
                raise ConnectionError("mail server unreachable")
            return a_brief(principal.user_id)
        finally:
            self.concurrent -= 1


def build(generator=None, **kwargs) -> Scheduler:
    # `store or BriefStore()` would be a bug: BriefStore defines __len__, so an
    # empty store is falsy and the caller's store would be silently discarded.
    store = kwargs.pop("store", None)
    return Scheduler(
        generator=generator or StubGenerator(),
        store=BriefStore() if store is None else store,
        principal_for=principal_for,
        **kwargs,
    )


# -- schedule arithmetic ---------------------------------------------------


def test_next_run_is_the_same_day_when_still_ahead() -> None:
    schedule = Schedule(at=time(7, 30), jitter_s=0)

    assert schedule.next_run_after(MONDAY_6AM) == datetime(2026, 7, 27, 7, 30, tzinfo=UTC)


def test_next_run_rolls_to_tomorrow_once_past() -> None:
    schedule = Schedule(at=time(7, 30), jitter_s=0)

    nxt = schedule.next_run_after(FRIDAY_9AM)

    # Friday 09:00 → next weekday slot is Monday.
    assert nxt.weekday() == 0
    assert nxt.hour == 7


def test_weekends_are_skipped() -> None:
    schedule = Schedule(at=time(7, 30), days=WEEKDAYS, jitter_s=0)

    assert schedule.next_run_after(SATURDAY_9AM).weekday() == 0


def test_timezone_is_respected() -> None:
    """A 'morning' brief computed in UTC arrives overnight for half an org."""
    istanbul = Schedule(at=time(7, 30), timezone="Europe/Istanbul", jitter_s=0)

    nxt = istanbul.next_run_after(MONDAY_6AM)

    assert nxt.hour == 7
    assert nxt.utcoffset() == timedelta(hours=3)


def test_jitter_spreads_users_apart() -> None:
    """500 users at 08:00 would serialise; the last would get lunchtime news."""
    schedule = Schedule(at=time(7, 30), jitter_s=900)

    times = {schedule.next_run_after(MONDAY_6AM, user_id=f"user{i}").time() for i in range(50)}

    assert len(times) > 20


def test_jitter_is_stable_for_a_user() -> None:
    """A brief that lands at 07:31 then 07:52 is one people stop relying on."""
    schedule = Schedule(at=time(7, 30), jitter_s=900)

    first = schedule.next_run_after(MONDAY_6AM, user_id="alice")
    second = schedule.next_run_after(MONDAY_6AM + timedelta(days=7), user_id="alice")

    assert first.time() == second.time()


def test_jitter_stays_within_its_window() -> None:
    schedule = Schedule(at=time(7, 30), jitter_s=900)

    for i in range(100):
        run = schedule.next_run_after(MONDAY_6AM, user_id=f"u{i}")
        assert time(7, 30) <= run.time() <= time(7, 45)


# -- due-ness --------------------------------------------------------------


def test_a_new_job_is_not_immediately_due() -> None:
    """Deploying at 15:00 must not fire everyone's morning brief on the spot."""
    job = ScheduledJob(user_id="alice", schedule=Schedule(at=time(7, 30), jitter_s=0))

    assert not job.is_due(datetime(2026, 7, 27, 15, 0, tzinfo=UTC))


def test_a_job_is_due_once_its_time_has_passed() -> None:
    job = ScheduledJob(user_id="alice", schedule=Schedule(at=time(7, 30), jitter_s=0))
    job.last_run = datetime(2026, 7, 26, 7, 30, tzinfo=UTC)

    assert job.is_due(datetime(2026, 7, 27, 8, 0, tzinfo=UTC))


def test_a_job_is_not_due_twice_the_same_day() -> None:
    job = ScheduledJob(user_id="alice", schedule=Schedule(at=time(7, 30), jitter_s=0))
    job.last_run = datetime(2026, 7, 27, 7, 30, tzinfo=UTC)

    assert not job.is_due(datetime(2026, 7, 27, 11, 0, tzinfo=UTC))


def test_a_disabled_job_is_never_due() -> None:
    job = ScheduledJob(user_id="alice", enabled=False)
    job.last_run = datetime(2026, 1, 1, tzinfo=UTC)

    assert not job.is_due(FRIDAY_9AM)


# -- running ---------------------------------------------------------------


async def test_a_due_job_generates_and_stores() -> None:
    generator = StubGenerator()
    store = BriefStore()
    scheduler = build(generator, store=store, clock=lambda: FRIDAY_9AM)
    job = scheduler.add(ScheduledJob(user_id="alice", schedule=Schedule(jitter_s=0)))
    job.last_run = FRIDAY_9AM - timedelta(days=1)

    assert await scheduler.tick() == 1
    assert store.get("alice") is not None
    assert generator.calls == [("alice", "Good morning")]


async def test_nothing_runs_when_nothing_is_due() -> None:
    generator = StubGenerator()
    scheduler = build(generator, clock=lambda: MONDAY_6AM)
    scheduler.add(ScheduledJob(user_id="alice"))

    assert await scheduler.tick() == 0
    assert generator.calls == []


async def test_one_users_failure_does_not_stop_another() -> None:
    generator = StubGenerator(fail_for={"alice"})
    store = BriefStore()
    scheduler = build(generator, store=store, clock=lambda: FRIDAY_9AM)
    for user in ("alice", "bob"):
        job = scheduler.add(ScheduledJob(user_id=user, schedule=Schedule(jitter_s=0)))
        job.last_run = FRIDAY_9AM - timedelta(days=1)

    generated = await scheduler.tick()

    assert generated == 1
    assert store.get("bob") is not None
    assert store.get("alice") is None


async def test_a_failure_is_recorded_on_the_job() -> None:
    generator = StubGenerator(fail_for={"alice"})
    scheduler = build(generator, clock=lambda: FRIDAY_9AM)
    job = scheduler.add(ScheduledJob(user_id="alice", schedule=Schedule(jitter_s=0)))
    job.last_run = FRIDAY_9AM - timedelta(days=1)

    await scheduler.tick()

    assert job.failures == 1
    assert "ConnectionError" in (job.last_error or "")


async def test_a_failed_job_does_not_retry_on_the_next_tick() -> None:
    """Otherwise a dead connector becomes a tight loop against the model plane."""
    generator = StubGenerator(fail_for={"alice"})
    scheduler = build(generator, clock=lambda: FRIDAY_9AM)
    job = scheduler.add(ScheduledJob(user_id="alice", schedule=Schedule(jitter_s=0)))
    job.last_run = FRIDAY_9AM - timedelta(days=1)

    await scheduler.tick()
    await scheduler.tick()

    assert len(generator.calls) == 1


async def test_concurrency_is_bounded() -> None:
    """Proactive work must yield to people waiting on interactive requests."""
    generator = StubGenerator()
    scheduler = build(generator, clock=lambda: FRIDAY_9AM, max_concurrency=2)
    for i in range(10):
        job = scheduler.add(ScheduledJob(user_id=f"u{i}", schedule=Schedule(jitter_s=0)))
        job.last_run = FRIDAY_9AM - timedelta(days=1)

    await scheduler.tick()

    assert generator.max_concurrent <= 2
    assert len(generator.calls) == 10


async def test_evening_jobs_use_their_own_greeting() -> None:
    generator = StubGenerator()
    scheduler = build(generator, clock=lambda: FRIDAY_9AM)
    job = scheduler.add(
        ScheduledJob(user_id="alice", kind=JobKind.EVENING_SUMMARY, schedule=Schedule(jitter_s=0))
    )
    job.last_run = FRIDAY_9AM - timedelta(days=1)

    await scheduler.tick()

    assert generator.calls == [("alice", "Good evening")]


async def test_adding_a_job_twice_replaces_it() -> None:
    scheduler = build()
    scheduler.add(ScheduledJob(user_id="alice"))
    scheduler.add(ScheduledJob(user_id="alice", enabled=False))

    assert len(scheduler.for_user("alice")) == 1
    assert not scheduler.for_user("alice")[0].enabled


# -- the store -------------------------------------------------------------


def test_store_returns_the_latest_brief() -> None:
    store = BriefStore()
    store.put("alice", a_brief())

    assert store.get("alice").brief.body == "Your brief."


def test_stale_briefs_are_withheld() -> None:
    """Yesterday's brief describes a world that has moved on."""
    store = BriefStore()
    store.put("alice", a_brief())

    stale = store.get(
        "alice", now=datetime.now(UTC) + timedelta(hours=20), max_age=timedelta(hours=12)
    )

    assert stale is None


def test_fresh_briefs_are_served() -> None:
    store = BriefStore()
    store.put("alice", a_brief())

    fresh = store.get(
        "alice", now=datetime.now(UTC) + timedelta(minutes=30), max_age=timedelta(hours=12)
    )

    assert fresh is not None


def test_store_is_per_user() -> None:
    store = BriefStore()
    store.put("alice", a_brief())

    assert store.get("bob") is None


# -- lifecycle -------------------------------------------------------------


async def test_the_loop_survives_a_failing_tick() -> None:
    scheduler = build(clock=lambda: FRIDAY_9AM)

    calls = {"n": 0}

    async def exploding_tick() -> int:
        calls["n"] += 1
        raise RuntimeError("bad tick")

    scheduler.tick = exploding_tick  # type: ignore[method-assign]
    scheduler.start(interval_s=0.01)
    import asyncio

    await asyncio.sleep(0.06)
    await scheduler.stop()

    assert calls["n"] > 1


async def test_stop_is_idempotent() -> None:
    scheduler = build()
    await scheduler.stop()
    await scheduler.stop()


@pytest.mark.parametrize("kind", list(JobKind))
def test_every_job_kind_has_a_greeting(kind: JobKind) -> None:
    from uione.proactive.scheduler import _greeting

    assert _greeting(kind)
