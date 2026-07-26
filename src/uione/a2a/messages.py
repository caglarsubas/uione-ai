"""A2A message types and the agent directory.

Modelled on the shape of the Linux Foundation A2A specification — an agent card
describing who an assistant acts for and what it can do, and typed requests
between them — but kept behind our own types so spec churn stays contained to an
adapter. The specification reached 1.0 recently; committing our whole internal
model to it now would be optimistic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from uione.a2a.contracts import Facet


class Capability(StrEnum):
    """What an assistant will answer."""

    AVAILABILITY = "availability"
    WORKLOAD = "workload"
    TASK_STATUS = "task_status"
    MEETING_PROPOSAL = "meeting_proposal"
    TASK_DELEGATION = "task_delegation"


@dataclass
class AgentCard:
    """One employee's assistant, as other assistants see it."""

    agent_id: str
    owner_id: str
    display_name: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    organisation: str = "internal"
    timezone: str = "UTC"

    @property
    def external(self) -> bool:
        return self.organisation != "internal"

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities


class RequestKind(StrEnum):
    ASK_AVAILABILITY = "ask_availability"
    ASK_WORKLOAD = "ask_workload"
    ASK_TASK_STATUS = "ask_task_status"

    PROPOSE_MEETING = "propose_meeting"
    """A commitment. Never answered without the owner's approval."""

    DELEGATE_TASK = "delegate_task"
    """Also a commitment: accepting work on someone's behalf."""


#: What each request kind needs to reveal in order to be answered at all.
REQUIRED_FACETS: dict[RequestKind, frozenset[Facet]] = {
    RequestKind.ASK_AVAILABILITY: frozenset({Facet.FREE_BUSY, Facet.OUT_OF_OFFICE}),
    RequestKind.ASK_WORKLOAD: frozenset({Facet.WORKLOAD}),
    RequestKind.ASK_TASK_STATUS: frozenset({Facet.TASK_STATUS}),
    RequestKind.PROPOSE_MEETING: frozenset({Facet.FREE_BUSY}),
    RequestKind.DELEGATE_TASK: frozenset({Facet.WORKLOAD}),
}

#: Requests that bind the owner to something. These are held for a human
#: regardless of contract, because a contract governs *disclosure*, and agreeing
#: to attend a meeting or take on work is not disclosure.
COMMITMENTS = frozenset({RequestKind.PROPOSE_MEETING, RequestKind.DELEGATE_TASK})


@dataclass
class A2ARequest:
    from_agent: str
    to_agent: str
    kind: RequestKind
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    delegation_chain: tuple[str, ...] = ()
    """Every agent this request passed through, oldest first.

    Without it, "who asked for this?" is unanswerable two hops in — and an
    assistant acting on a request that originated three colleagues away is
    exactly the situation an auditor will ask about.
    """

    @property
    def is_commitment(self) -> bool:
        return self.kind in COMMITMENTS

    def forwarded_by(self, agent_id: str) -> A2ARequest:
        # The full path so far includes the current sender, not just the earlier
        # chain — otherwise an agent can forward to itself indefinitely, since it
        # is not yet recorded in the chain it is about to be appended to.
        if agent_id in (*self.delegation_chain, self.from_agent):
            # A cycle would otherwise pass requests forever between two obliging
            # assistants, each thinking it is helping.
            raise ValueError(f"delegation cycle: {agent_id} already in chain")
        return A2ARequest(
            from_agent=agent_id,
            to_agent=self.to_agent,
            kind=self.kind,
            payload=dict(self.payload),
            id=self.id,
            delegation_chain=(*self.delegation_chain, self.from_agent),
        )

    def provenance(self) -> str:
        chain = [*self.delegation_chain, self.from_agent]
        return " → ".join(chain)


class Outcome(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    """Answered, but a contract withheld some of what was asked."""

    REFUSED = "refused"
    """Nothing could be disclosed, or the capability is not offered."""

    HELD = "held"
    """A commitment awaiting the owner's approval."""


@dataclass
class A2AResponse:
    request_id: str
    from_agent: str
    outcome: Outcome
    data: dict = field(default_factory=dict)
    withheld_note: str = ""
    reason: str = ""
    pending_action_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.ANSWERED, Outcome.PARTIAL)

    def render(self) -> str:
        """A sentence for the requesting assistant to relay to its user."""
        if self.outcome is Outcome.HELD:
            return f"{self.from_agent} has passed this to their user for approval."
        if self.outcome is Outcome.REFUSED:
            return f"{self.from_agent} declined: {self.reason}"
        text = str(self.data.get("summary") or self.data)
        if self.withheld_note:
            text += f" ({self.withheld_note})"
        return text


class AgentDirectory:
    """Who's who. Keyed by agent id, resolvable by the person behind it."""

    def __init__(self) -> None:
        self._cards: dict[str, AgentCard] = {}

    def register(self, card: AgentCard) -> AgentCard:
        self._cards[card.agent_id] = card
        return card

    def get(self, agent_id: str) -> AgentCard | None:
        return self._cards.get(agent_id)

    def for_owner(self, owner_id: str) -> AgentCard | None:
        return next((c for c in self._cards.values() if c.owner_id == owner_id), None)

    def all(self) -> list[AgentCard]:
        return list(self._cards.values())

    @staticmethod
    def agent_id_for(owner_id: str) -> str:
        return f"agent:{owner_id}"
