"""Deciding who waits for the GPU.

Measured against a real 8B model on one machine, asking the same question:

| concurrent requests | wall clock | slowest reply |
|---|---|---|
| 1 | 1.2s | 1.2s |
| 3 | 3.0s | 3.0s |
| 6 | 7.0s | 7.0s |

Six requests take roughly six times one request. **Adding concurrency adds no
throughput.** The engine serialises internally whatever arrives, so sending more
at once does not get more done — it only spreads the same total time across more
people, and everybody waits for everybody.

That reframes the problem. This is not a limiter protecting the engine from
overload; the engine protects itself perfectly well by queueing. It is
**admission control**: since the total time is fixed, the only decision left is
who spends it.

The queue inside the engine is FIFO and priority-blind. Five morning briefs
submitted at 07:29 will delay a question asked at 07:30, and the person waiting
on the answer has no idea they are behind work nobody asked for. Holding
background work in *our* queue instead lets interactive requests reach the engine
first. Throughput is identical — the latency simply lands on the work with nobody
watching it.

**Interactive is the default, deliberately.** A caller who forgets to declare
priority is treated as user-facing. The inverse default would let a forgotten
annotation starve somebody's chat behind a batch job, and of the two mistakes
that is much the worse one to make quietly.

**Waiting has a deadline.** A request that cannot get a slot within the timeout
is refused, and the caller says the assistant is busy. Ninety seconds of spinner
followed by an answer is a worse experience than five seconds followed by "try
again" — the first teaches people the product is slow, the second that it is
loaded.

**Background work is not starved, only deferred.** Its wait is long rather than
infinite, and a scheduler whose job misses its slot retries on the next tick.
Late is acceptable for a brief; never is not.
"""

from __future__ import annotations

import asyncio
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum

import structlog

log = structlog.get_logger(__name__)


class Priority(IntEnum):
    """Lower sorts first."""

    INTERACTIVE = 0
    """Somebody is watching a spinner."""

    BACKGROUND = 1
    """A scheduler produced it and nobody is waiting."""


class ModelPlaneBusy(RuntimeError):
    """No slot became free within the caller's deadline."""


@dataclass
class _Waiter:
    priority: Priority
    #: Tie-break within a priority, so equal-priority callers are served in
    #: arrival order rather than by whatever the heap happens to do. Without it
    #: a steady trickle of interactive requests could reorder among themselves,
    #: which is invisible but unfair.
    sequence: int
    future: asyncio.Future

    def __lt__(self, other: _Waiter) -> bool:
        return (self.priority, self.sequence) < (other.priority, other.sequence)


@dataclass
class GateStats:
    admitted: int = 0
    refused: int = 0
    deferred: int = 0
    """Times a request had to wait at all — the signal that the engine is the
    bottleneck, which is worth knowing before anyone tunes anything else."""

    peak_queue: int = 0


@dataclass
class AdmissionGate:
    """A semaphore that knows which requests have somebody waiting on them."""

    #: How many requests may be in flight at the engine. Two by default: the
    #: measurement above shows a third adds nothing but latency, and one would
    #: leave the engine idle during our own overhead between requests.
    limit: int = 2

    #: How long an interactive caller waits before being told the engine is
    #: busy.
    interactive_timeout_s: float = 30.0

    #: Background work waits far longer, because being late costs nothing and
    #: a refusal costs a whole brief.
    background_timeout_s: float = 300.0

    stats: GateStats = field(default_factory=GateStats)

    _active: int = 0
    _waiters: list[_Waiter] = field(default_factory=list)
    _counter: itertools.count = field(default_factory=itertools.count)

    @property
    def in_flight(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    @asynccontextmanager
    async def slot(self, priority: Priority = Priority.INTERACTIVE):
        """Hold a slot for the duration of one request.

        A context manager rather than acquire/release, because a streamed
        completion holds its slot for as long as the stream runs and an early
        `return` in the consumer must still give it back.
        """
        await self._acquire(priority)
        try:
            yield
        finally:
            self._release()

    async def _acquire(self, priority: Priority) -> None:
        if self._active < self.limit and not self._waiters:
            # Free capacity and nobody ahead. Checking the queue matters:
            # admitting straight through while others wait would let a busy
            # period starve whoever arrived first.
            self._active += 1
            self.stats.admitted += 1
            return

        waiter = _Waiter(priority, next(self._counter), asyncio.get_running_loop().create_future())
        self._waiters.append(waiter)
        self._waiters.sort()
        self.stats.deferred += 1
        self.stats.peak_queue = max(self.stats.peak_queue, len(self._waiters))

        timeout = (
            self.interactive_timeout_s
            if priority is Priority.INTERACTIVE
            else self.background_timeout_s
        )
        try:
            await asyncio.wait_for(waiter.future, timeout=timeout)
        except TimeoutError:
            # Removed under the same lock-free discipline as everything else
            # here: this is single-threaded asyncio, and every mutation happens
            # between awaits.
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            self.stats.refused += 1
            log.warning(
                "modelplane.refused",
                priority=priority.name.lower(),
                waited_s=timeout,
                in_flight=self._active,
            )
            raise ModelPlaneBusy(
                f"the model plane is busy: no capacity within {timeout:g}s. "
                "The engine serves a fixed number of requests at a time; try again."
            ) from None

        self.stats.admitted += 1

    def _release(self) -> None:
        self._active -= 1
        while self._waiters and self._active < self.limit:
            waiter = self._waiters.pop(0)
            if waiter.future.done():
                # Timed out between being woken and being read. Its slot is not
                # consumed, so the loop continues to the next in line.
                continue
            self._active += 1
            waiter.future.set_result(None)
            return


#: A gate that admits everything, for tests and for a deployment whose engine
#: genuinely scales — a hosted endpoint behind an autoscaler has no reason to
#: queue here.
UNLIMITED = AdmissionGate(limit=10_000)
