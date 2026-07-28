"""Admission control in front of the model plane.

The measurement this exists to act on: an 8B model served six concurrent
requests in about six times the time it served one. Concurrency buys no
throughput, so the only decision left is who waits — and the person watching a
spinner should not be behind five briefs nobody asked for.
"""

from __future__ import annotations

import asyncio

import pytest

from uione.modelplane import AdmissionGate, ModelPlaneBusy, Priority


async def test_a_free_gate_admits_immediately() -> None:
    gate = AdmissionGate(limit=2)

    async with gate.slot():
        assert gate.in_flight == 1

    assert gate.in_flight == 0


async def test_capacity_is_bounded() -> None:
    gate = AdmissionGate(limit=2)
    released = asyncio.Event()
    seen: list[int] = []

    async def hold() -> None:
        async with gate.slot():
            seen.append(gate.in_flight)
            await released.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(4)]
    await asyncio.sleep(0.05)

    assert gate.in_flight == 2, "a third request must not reach the engine"
    assert gate.queued == 2

    released.set()
    await asyncio.gather(*tasks)
    assert max(seen) == 2


async def test_an_interactive_request_overtakes_background_work() -> None:
    """The whole point. Measured against a real engine: a question behind five
    briefs took 6.4s ungated and 3.0s gated, and the time moved onto the work
    with nobody watching it."""
    gate = AdmissionGate(limit=1)
    order: list[str] = []
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot(Priority.BACKGROUND):
            await release.wait()

    async def queued(priority: Priority, label: str) -> None:
        async with gate.slot(priority):
            order.append(label)

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)

    # Background queues first, then the person asks.
    waiters = [
        asyncio.create_task(queued(Priority.BACKGROUND, "background-1")),
        asyncio.create_task(queued(Priority.BACKGROUND, "background-2")),
    ]
    await asyncio.sleep(0.02)
    waiters.append(asyncio.create_task(queued(Priority.INTERACTIVE, "interactive")))
    await asyncio.sleep(0.02)

    release.set()
    await asyncio.gather(holder, *waiters)

    assert order[0] == "interactive", "the person waiting must be served first"


async def test_priority_is_not_preemption() -> None:
    """A request already at the engine cannot be recalled. An interactive
    caller still waits for a slot to free — it just gets the next one."""
    gate = AdmissionGate(limit=1)
    release = asyncio.Event()
    started = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot(Priority.BACKGROUND):
            started.set()
            await release.wait()

    holder = asyncio.create_task(occupy())
    await started.wait()

    interactive = asyncio.create_task(_enter(gate, Priority.INTERACTIVE))
    await asyncio.sleep(0.05)

    assert not interactive.done(), "it cannot jump a request already in flight"

    release.set()
    await asyncio.gather(holder, interactive)


async def _enter(gate: AdmissionGate, priority: Priority) -> None:
    async with gate.slot(priority):
        return


async def test_equal_priority_is_served_in_arrival_order() -> None:
    """Without the tie-break, equal-priority callers reorder among themselves —
    invisible, and unfair."""
    gate = AdmissionGate(limit=1)
    order: list[int] = []
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot():
            await release.wait()

    async def queued(n: int) -> None:
        async with gate.slot(Priority.INTERACTIVE):
            order.append(n)

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)

    waiters = []
    for n in range(4):
        waiters.append(asyncio.create_task(queued(n)))
        await asyncio.sleep(0.01)

    release.set()
    await asyncio.gather(holder, *waiters)

    assert order == [0, 1, 2, 3]


async def test_a_waiting_interactive_request_is_refused_rather_than_hung() -> None:
    """Ninety seconds of spinner teaches people the product is slow. A quick
    refusal teaches them it is loaded, which is recoverable."""
    gate = AdmissionGate(limit=1, interactive_timeout_s=0.05)
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot():
            await release.wait()

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)

    with pytest.raises(ModelPlaneBusy, match="busy"):
        async with gate.slot(Priority.INTERACTIVE):
            pass

    release.set()
    await holder


async def test_background_work_waits_far_longer_than_interactive() -> None:
    """Late is acceptable for a brief; never is not."""
    gate = AdmissionGate(limit=1, interactive_timeout_s=0.05, background_timeout_s=5.0)
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot():
            await release.wait()

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)
    background = asyncio.create_task(_enter(gate, Priority.BACKGROUND))

    # The interactive timeout would have fired by now.
    await asyncio.sleep(0.2)
    assert not background.done()

    release.set()
    await asyncio.gather(holder, background)


async def test_a_refused_request_does_not_leak_a_slot() -> None:
    """A timed-out waiter that stayed in the queue would shrink capacity every
    time the engine got busy, until nothing ran at all."""
    gate = AdmissionGate(limit=1, interactive_timeout_s=0.05)
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot():
            await release.wait()

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)

    for _ in range(3):
        with pytest.raises(ModelPlaneBusy):
            async with gate.slot(Priority.INTERACTIVE):
                pass

    release.set()
    await holder

    assert gate.queued == 0
    assert gate.in_flight == 0
    async with gate.slot():
        assert gate.in_flight == 1


async def test_an_exception_inside_a_slot_still_releases_it() -> None:
    gate = AdmissionGate(limit=1)

    with pytest.raises(ValueError):
        async with gate.slot():
            raise ValueError("boom")

    assert gate.in_flight == 0
    async with gate.slot():
        assert gate.in_flight == 1


async def test_the_queue_is_not_jumped_when_capacity_is_free() -> None:
    """Admitting straight through while others wait would let a busy period
    starve whoever arrived first."""
    gate = AdmissionGate(limit=1)
    order: list[str] = []
    release = asyncio.Event()

    async def occupy() -> None:
        async with gate.slot():
            await release.wait()

    async def queued(label: str) -> None:
        async with gate.slot(Priority.INTERACTIVE):
            order.append(label)
            await asyncio.sleep(0.01)

    holder = asyncio.create_task(occupy())
    await asyncio.sleep(0.02)
    waiting = asyncio.create_task(queued("waited"))
    await asyncio.sleep(0.02)

    release.set()
    await asyncio.sleep(0)
    latecomer = asyncio.create_task(queued("arrived-later"))

    await asyncio.gather(holder, waiting, latecomer)

    assert order == ["waited", "arrived-later"]


def test_statistics_show_whether_the_engine_is_the_bottleneck() -> None:
    """`deferred` is the number worth watching: it says requests are queueing,
    which is the signal to add hardware rather than tune anything else."""
    gate = AdmissionGate(limit=2)

    assert gate.stats.admitted == 0
    assert gate.stats.deferred == 0
    assert gate.stats.refused == 0


async def test_the_unlimited_gate_admits_everything() -> None:
    """For an engine that genuinely scales — a hosted endpoint behind an
    autoscaler has no reason to queue here."""
    from uione.modelplane import UNLIMITED

    async with UNLIMITED.slot(), UNLIMITED.slot():
        assert UNLIMITED.in_flight >= 2
