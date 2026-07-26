"""The A2A bus — where one assistant asks another, and governance applies.

Every request passes three gates, in this order:

1. **Capability** — does the receiving assistant answer this kind of question at
   all? Cheapest check, and refusing here reveals nothing about the owner.
2. **Disclosure contract** — of what was asked, what may this requester see? What
   cannot is withheld *and reported*.
3. **Commitment** — proposing a meeting or delegating work binds the owner to
   something, so it is held for their approval regardless of contract. A contract
   governs disclosure; agreeing to attend a meeting is not disclosure.

Everything is audited with the full delegation chain, because "Alice's assistant
asked" and "Alice's assistant asked on behalf of someone two hops away" are
different events and only one of them is benign.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from uione.a2a.contracts import ContractRegistry, Facet
from uione.a2a.messages import (
    REQUIRED_FACETS,
    A2ARequest,
    A2AResponse,
    AgentCard,
    AgentDirectory,
    Capability,
    Outcome,
    RequestKind,
)
from uione.mcphub import Principal

log = structlog.get_logger(__name__)

#: What each request kind needs the receiving assistant to be able to do.
_CAPABILITY_FOR: dict[RequestKind, Capability] = {
    RequestKind.ASK_AVAILABILITY: Capability.AVAILABILITY,
    RequestKind.ASK_WORKLOAD: Capability.WORKLOAD,
    RequestKind.ASK_TASK_STATUS: Capability.TASK_STATUS,
    RequestKind.PROPOSE_MEETING: Capability.MEETING_PROPOSAL,
    RequestKind.DELEGATE_TASK: Capability.TASK_DELEGATION,
}

Answerer = Callable[[AgentCard, A2ARequest, frozenset[Facet]], Awaitable[dict]]
"""Produces the data for a request, given only the facets it may reveal.

The signature is the enforcement: an answerer is *handed* its permitted facets
rather than being trusted to check them, so it cannot accidentally return a
meeting subject when only free/busy was granted.
"""


class A2ABus:
    def __init__(
        self,
        *,
        directory: AgentDirectory,
        contracts: ContractRegistry,
        answerer: Answerer,
        approvals: Any | None = None,
        audit: Any | None = None,
        principal_for: Callable[[str], Principal] | None = None,
    ) -> None:
        self._directory = directory
        self._contracts = contracts
        self._answerer = answerer
        self._approvals = approvals
        self._audit = audit
        self._principal_for = principal_for or (
            lambda user_id: Principal(user_id=user_id, roles=frozenset())
        )

    async def send(self, request: A2ARequest, *, requester_roles: frozenset[str]) -> A2AResponse:
        target = self._directory.get(request.to_agent)
        if target is None:
            return A2AResponse(
                request_id=request.id,
                from_agent=request.to_agent,
                outcome=Outcome.REFUSED,
                reason="no such assistant",
            )

        requester = self._directory.get(request.from_agent)
        requester_owner = requester.owner_id if requester else request.from_agent

        # 1. Capability.
        needed = _CAPABILITY_FOR[request.kind]
        if not target.can(needed):
            await self._record(request, target, Outcome.REFUSED, "capability not offered")
            return A2AResponse(
                request_id=request.id,
                from_agent=target.agent_id,
                outcome=Outcome.REFUSED,
                reason=f"{target.display_name} does not answer {needed.value} requests",
            )

        # 2. Disclosure.
        disclosure = self._contracts.evaluate(
            owner_id=target.owner_id,
            requester_id=requester_owner,
            requester_roles=requester_roles,
            requested=REQUIRED_FACETS[request.kind],
            external=bool(requester and requester.external),
        )

        if disclosure.is_empty:
            await self._record(request, target, Outcome.REFUSED, "disclosure policy")
            return A2AResponse(
                request_id=request.id,
                from_agent=target.agent_id,
                outcome=Outcome.REFUSED,
                reason=disclosure.explain_withheld() or "their disclosure policy shares nothing",
            )

        # 3. Commitment. Checked after disclosure so a proposal that could never
        # be answered is refused rather than queued for a human to reject.
        if request.is_commitment:
            return await self._hold(request, target, disclosure)

        data = await self._answerer(target, request, disclosure.granted)
        outcome = Outcome.PARTIAL if disclosure.withheld else Outcome.ANSWERED
        await self._record(request, target, outcome, disclosure.explain_withheld())

        return A2AResponse(
            request_id=request.id,
            from_agent=target.agent_id,
            outcome=outcome,
            data=data,
            withheld_note=disclosure.explain_withheld(),
        )

    async def _hold(self, request: A2ARequest, target: AgentCard, disclosure) -> A2AResponse:
        """Queue a commitment for the owner."""
        pending_id = None
        if self._approvals is not None:
            from uione.a2a.commitments import commitment_spec, render_commitment

            action = await self._approvals.submit(
                self._principal_for(target.owner_id),
                commitment_spec(request.kind),
                {
                    "requested_by": request.provenance(),
                    "kind": str(request.kind),
                    **request.payload,
                },
                reason=render_commitment(request),
            )
            pending_id = action.id

        await self._record(request, target, Outcome.HELD, "commitment requires owner approval")
        return A2AResponse(
            request_id=request.id,
            from_agent=target.agent_id,
            outcome=Outcome.HELD,
            pending_action_id=pending_id,
            withheld_note=disclosure.explain_withheld(),
        )

    async def _record(
        self, request: A2ARequest, target: AgentCard, outcome: Outcome, detail: str
    ) -> None:
        log.info(
            "a2a.request",
            request_id=request.id,
            kind=str(request.kind),
            chain=request.provenance(),
            to=target.agent_id,
            outcome=str(outcome),
            detail=detail,
        )
        if self._audit is None:
            return

        from uione.mcphub import AuditOutcome, RiskClass

        await self._audit.record(
            principal=self._principal_for(target.owner_id),
            server="a2a",
            tool=f"a2a.{request.kind}",
            risk=RiskClass.READ if not request.is_commitment else RiskClass.REVERSIBLE_WRITE,
            outcome={
                Outcome.ANSWERED: AuditOutcome.ALLOWED,
                Outcome.PARTIAL: AuditOutcome.ALLOWED,
                Outcome.REFUSED: AuditOutcome.DENIED,
                Outcome.HELD: AuditOutcome.HELD_FOR_APPROVAL,
            }[outcome],
            # The chain, not just the immediate sender: an assistant acting on a
            # request that originated three colleagues away is the case an
            # auditor will ask about.
            arguments={"chain": request.provenance(), **request.payload},
            detail=detail,
            correlation_id=request.id,
        )
