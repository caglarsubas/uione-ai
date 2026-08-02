"""The unified action queue — feature F6.3.

Everything awaiting this person, across every system, in one ranked list.

`ARCHITECTURE.md` has described the API as serving a "chat, brief, action queue"
for some time. The first two existed.

**No model runs here.** The brief is prose and costs a GPU call; this is a list
of records and costs none. That is not an optimisation, it is the difference
between a surface you open once a morning and one you can leave open all day —
and it means the queue cannot hallucinate an item, misreport a state, or invent
a due date, which are the three things `docs/EVALS.md` records models doing to
exactly this data.

**It is a queue, not a second inbox.** Gap G7 names the failure mode: aggregate
every system's notifications and you have built a worse inbox than the ones you
replaced. Three things keep it a queue:

*Cross-system deduplication.* One incident that also produced a chat message and
a ticket is **one** item with three sources, not three items. The work graph
already resolves shared identifiers deterministically, so this is that capability
finally reaching a surface rather than a new guess.

*A cap.* Twenty items is a queue; two hundred is a backlog, and nobody triages a
backlog. What was dropped is reported rather than silently truncated.

*An explicit reason per item.* Every entry says why it is there and why it sits
where it does. A ranked list nobody can explain is one people stop trusting the
moment its top item is wrong, and they cannot tell you what "wrong" would mean.

**Ranking is a rule, not a score.** No weights, no learned model. An item's rank
is a small ordered sequence of facts about it, so "why is this above that" always
has an answer in words. Learned ranking needs the feedback store (F8.7) and a
reason to believe it beats this, and neither exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

import structlog

from uione.mcphub import McpGateway, Principal

log = structlog.get_logger(__name__)

#: Items shown. More than this is a backlog — see the module docstring.
DEFAULT_LIMIT = 20


class Urgency(IntEnum):
    """Why an item is where it is. Lower sorts first.

    An enum rather than a number so the reason survives into the API and the UI:
    the client shows the band, not a score it would have to explain.
    """

    BLOCKING_THE_ASSISTANT = 0
    """The assistant is waiting on you. Nothing else can be more urgent —
    it is work already prepared, stopped one click short of done."""

    CRITICAL_INCIDENT = 1
    """A P1/P2 incident assigned to you."""

    ASSIGNED_WORK = 2
    """A ticket or incident that is yours."""

    AWAITING_REPLY = 3
    """Someone is waiting on a response."""

    @property
    def label(self) -> str:
        return self.name.lower().replace("_", " ")


@dataclass
class QueueItem:
    """One thing awaiting this person."""

    key: str
    title: str
    urgency: Urgency
    reason: str
    """Why it is here, in the words a user would use."""

    sources: list[str] = field(default_factory=list)
    """Every system this item came from. More than one means it was deduplicated."""

    updated_at: str | None = None
    url: str | None = None
    action_id: str | None = None
    """Set when the item is an approval, so the client can act on it directly."""

    @property
    def cross_system(self) -> bool:
        return len(self.sources) > 1

    def sort_key(self) -> tuple:
        """Ordered facts, not a score.

        Urgency first, then how long it has been waiting — oldest first, because
        an item that has been ignored for three days is the one most likely to
        have been forgotten rather than deliberately deferred.
        """
        return (int(self.urgency), self.updated_at or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "urgency": self.urgency.label,
            "reason": self.reason,
            "sources": self.sources,
            "updated_at": self.updated_at,
            "url": self.url,
            "action_id": self.action_id,
            "cross_system": self.cross_system,
        }


@dataclass
class WorkQueue:
    """The assembled queue, and what it could not see."""

    items: list[QueueItem] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    """Systems that did not answer. Rendered, never swallowed — gap G8."""

    dropped: int = 0
    """Items beyond the cap. Reported so a short queue is never mistaken for a
    quiet one."""

    generated_at: str = ""

    @property
    def complete(self) -> bool:
        return not self.unavailable

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "unavailable": self.unavailable,
            "dropped": self.dropped,
            "complete": self.complete,
            "generated_at": self.generated_at,
        }


#: Where queue items come from, and how each becomes one.
#:
#: Every tool here answers "what is awaiting *me*", which is the only question a
#: queue may ask. `mail.list_unread` was excluded until it could: an unread
#: message is not an action, and a queue listing the whole mailbox has become the
#: inbox it was meant to replace. It now returns rows only for messages that are
#: unread, addressed to this person directly rather than copied, and not bulk —
#: see `connectors.mail.source.awaits_reply` for the rule and its blind spot.
_SOURCES = (
    ("tasks.my_open_issues", Urgency.ASSIGNED_WORK, "assigned to you"),
    ("incidents.my_incidents", Urgency.ASSIGNED_WORK, "assigned to you"),
    # The fixture estate names the same thing `active`. Listing both rather than
    # renaming one: the fixture's name is right for a fixture that has no notion
    # of "me", and ServiceNow's is right for a system that does.
    ("incidents.active", Urgency.ASSIGNED_WORK, "open and assigned to you"),
    ("mail.list_unread", Urgency.AWAITING_REPLY, "addressed to you and unanswered"),
)

#: ServiceNow priorities that outrank ordinary assigned work.
_CRITICAL_PRIORITIES = frozenset({"1", "2"})


class QueueBuilder:
    """Assembles the queue for one principal.

    Everything is fetched through the gateway, so the queue shows exactly what
    that person is permitted to see — the same guarantee retrieval makes, by the
    same mechanism, rather than a second implementation of it.
    """

    def __init__(self, gateway: McpGateway, *, limit: int = DEFAULT_LIMIT) -> None:
        self._gateway = gateway
        self._limit = limit

    async def build(
        self,
        principal: Principal,
        *,
        approvals: list | None = None,
        correlation_id: str | None = None,
    ) -> WorkQueue:
        queue = WorkQueue(generated_at=datetime.now(UTC).isoformat())

        # Approvals first, and from the store rather than a tool: they are our
        # own state, and routing them through a connector would be pretending
        # otherwise.
        for action in approvals or []:
            queue.items.append(
                QueueItem(
                    key=getattr(action, "tool", "action"),
                    title=_describe_action(action),
                    urgency=Urgency.BLOCKING_THE_ASSISTANT,
                    reason="your assistant prepared this and is waiting for you",
                    sources=["approvals"],
                    updated_at=_iso(getattr(action, "created_at", None)),
                    action_id=getattr(action, "id", None),
                )
            )

        for tool, urgency, reason in _SOURCES:
            if not self._gateway.has_tool(tool):
                # Not configured in this deployment. Skipped rather than reported
                # unavailable — "degraded" must mean a system we have is down, or
                # every queue in every deployment carries a permanent warning.
                continue

            call = await self._gateway.call(principal, tool, {}, correlation_id=correlation_id)
            if not call.ok:
                queue.unavailable.append(tool.split(".", 1)[0])
                continue

            for row in (call.result.structured or {}).get("items") or []:
                queue.items.append(_row_to_item(row, tool, urgency, reason))

        self._deduplicate(queue)
        queue.items.sort(key=QueueItem.sort_key)

        if len(queue.items) > self._limit:
            queue.dropped = len(queue.items) - self._limit
            queue.items = queue.items[: self._limit]

        log.info(
            "queue.built",
            principal=principal.user_id,
            items=len(queue.items),
            dropped=queue.dropped,
            unavailable=queue.unavailable,
        )
        return queue

    def _deduplicate(self, queue: WorkQueue) -> None:
        """Collapse items that are the same thing seen from two systems.

        Matched on the identifier, which is the work graph's deterministic rule
        rather than a similarity of titles. Two systems calling something
        `INC0010001` are talking about the same incident; two systems with
        similar-sounding titles are not necessarily, and guessing that they are
        is how a queue starts hiding work.
        """
        by_key: dict[str, QueueItem] = {}
        merged: list[QueueItem] = []

        for item in queue.items:
            existing = by_key.get(item.key)
            if existing is None:
                by_key[item.key] = item
                merged.append(item)
                continue

            for source in item.sources:
                if source not in existing.sources:
                    existing.sources.append(source)
            # The more urgent reading of the same thing wins: an incident that is
            # also a ticket is an incident.
            if item.urgency < existing.urgency:
                existing.urgency = item.urgency
                existing.reason = item.reason

        queue.items = merged


def _row_to_item(row: dict, tool: str, urgency: Urgency, reason: str) -> QueueItem:
    server = tool.split(".", 1)[0]
    priority = str(row.get("priority") or "")

    if priority in _CRITICAL_PRIORITIES:
        urgency = Urgency.CRITICAL_INCIDENT
        reason = f"P{priority} incident assigned to you"

    return QueueItem(
        key=str(row.get("key") or "?"),
        title=str(row.get("title") or "(no title)"),
        urgency=urgency,
        reason=reason,
        sources=[server],
        updated_at=row.get("updated_at"),
        url=row.get("url"),
    )


def _describe_action(action: Any) -> str:
    """A held action, in a line.

    Falls back to the tool name rather than rendering arguments: an approval's
    arguments can contain a whole email body, and a queue entry is a title.
    """
    tool = getattr(action, "tool", "an action")
    return f"Approve: {tool}"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
