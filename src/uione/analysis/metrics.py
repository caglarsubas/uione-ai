"""Counting what the estate looks like, once a day, so trends exist at all.

The anomaly detector needs a series. Nothing in this product produced one: every
connector answers "what is true now", and "now" compared against nothing is just
a number. This is the missing half — a small, boring daily census.

**What it counts, and why so little.** Open incidents, open tasks, open claims,
firing alerts, unread chat. Five numbers, all of them the *size of a queue*.
Queue sizes are the metrics where a change genuinely means something happened:
incidents doubling is a bad week, claims flatlining is a broken feed. Deeper
metrics — latency, revenue, conversion — belong to the systems that own them,
and re-deriving them here would produce a second set of numbers that disagrees
with the dashboard everyone already trusts.

**It counts what the *tool* returned, not what the vendor holds.** The count is
of records this principal can see, because that is the number they experience.
An estate-wide count would be a different metric wearing the same name, and the
day somebody compares the two, the assistant loses the argument.

**A failed source records nothing rather than zero.** This is the important one.
A connector outage that writes a 0 creates a cliff in the series, the detector
fires on it, and someone is told their incidents vanished overnight. A gap is
honest; a zero is a lie with a chart behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from uione.mcphub import McpGateway, Principal

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MetricSource:
    """One number, and where it comes from.

    ``key`` names the entry in the tool's structured result. Reading a count out
    of prose with a regular expression would make the metric a hostage to
    rendering changes — and the structured result exists precisely so that
    numbers never have to be parsed back out of a sentence.

    Named ``key`` rather than ``field`` because a dataclass attribute called
    ``field`` shadows ``dataclasses.field`` inside the class body, and the next
    default_factory in the same class fails with "'str' object is not callable".
    """

    metric: str
    tool: str
    key: str = "count"
    arguments: dict = field(default_factory=dict)
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or self.metric.replace("_", " ")


#: The daily census. Every entry is a queue size — see the module docstring for
#: why nothing deeper belongs here.
DEFAULT_METRICS: tuple[MetricSource, ...] = (
    MetricSource("open_incidents", "incidents.my_incidents", label="open incidents"),
    MetricSource("open_tasks", "tasks.my_open_issues", label="open tasks"),
    MetricSource("open_claims", "claims.my_claims", label="open claims"),
    MetricSource("firing_alerts", "bi.firing_alerts", label="firing alerts"),
    MetricSource("unread_chat", "chat.unread_messages", label="unread chat channels"),
    MetricSource("unread_mail", "mail.list_unread", label="unread mail"),
)


@dataclass
class Snapshot:
    """One census, with the failures named rather than folded into the numbers."""

    at: datetime
    values: dict[str, float] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unavailable


class MetricRecorder:
    """Takes the census and hands it to storage."""

    def __init__(
        self,
        gateway: McpGateway,
        *,
        sources: tuple[MetricSource, ...] = DEFAULT_METRICS,
        store=None,
    ) -> None:
        self._gateway = gateway
        self._sources = sources
        self._store = store

    def available(self) -> list[MetricSource]:
        """Only the metrics this deployment can actually produce.

        A source whose tool is not registered is absent, not zero — the same
        distinction the brief makes between a system that is down and one that
        was never installed.
        """
        return [s for s in self._sources if self._gateway.has_tool(s.tool)]

    async def take(self, principal: Principal, *, now: datetime | None = None) -> Snapshot:
        moment = now or datetime.now(UTC)
        snapshot = Snapshot(at=moment)

        for source in self.available():
            call = await self._gateway.call(principal, source.tool, dict(source.arguments))
            if not call.ok or call.result.structured is None:
                # Recorded as missing, never as zero. A zero here becomes a
                # cliff in the series, the detector fires on it, and somebody is
                # told their incidents vanished overnight.
                snapshot.unavailable.append(source.metric)
                log.info(
                    "metrics.source_unavailable",
                    metric=source.metric,
                    error=call.result.error,
                )
                continue

            value = call.result.structured.get(source.key)
            if not isinstance(value, int | float) or isinstance(value, bool):
                snapshot.unavailable.append(source.metric)
                log.warning("metrics.not_a_number", metric=source.metric, key=source.key, got=value)
                continue

            snapshot.values[source.metric] = float(value)

        if self._store is not None and snapshot.values:
            await self._store.record(principal.user_id, snapshot.values, at=moment)

        log.info(
            "metrics.snapshot",
            user=principal.user_id,
            recorded=len(snapshot.values),
            unavailable=len(snapshot.unavailable),
        )
        return snapshot

    def title_of(self, metric: str) -> str:
        for source in self._sources:
            if source.metric == metric:
                return source.title
        return metric.replace("_", " ")
