"""The governance plane — one object implementing the gateway's governor contract.

Composes the autonomy ladder, the approval queue, the undo journal, and egress
checks into the single decision the gateway asks for: *may this run now?*

Ordering inside :meth:`Governor.authorize` is deliberate. Egress is checked
before autonomy, so a mail to an unapproved domain is refused outright rather
than offered for approval — the user should not be handed a one-click button to
do the thing the policy forbids.
"""

from __future__ import annotations

import structlog

from uione.governance.approvals import ActionJournal, ApprovalStatus, ApprovalStore
from uione.governance.autonomy import AutonomyMode, AutonomyPolicy
from uione.governance.containment import EgressPolicy
from uione.mcphub import ActionContext, GovernanceVerdict, Principal, ToolResult, ToolSpec

log = structlog.get_logger(__name__)


class Governor:
    """Implements :class:`uione.mcphub.ActionGovernor`."""

    def __init__(
        self,
        *,
        autonomy: AutonomyPolicy | None = None,
        approvals: ApprovalStore | None = None,
        journal: ActionJournal | None = None,
        egress: EgressPolicy | None = None,
    ) -> None:
        self.autonomy = autonomy or AutonomyPolicy()
        self.approvals = approvals or ApprovalStore()
        self.journal = journal or ActionJournal()
        self.egress = egress or EgressPolicy(allow_all=True)

    async def authorize(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict,
        context: ActionContext,
    ) -> GovernanceVerdict:
        if not spec.mutating:
            return GovernanceVerdict(allowed=True, reason="read-only")

        # A previously approved action re-enters here to execute; it must not be
        # held a second time, but it must still be the action that was approved.
        if context.approved_action_id:
            action = await self.approvals.get(context.approved_action_id)
            if action and action.status is ApprovalStatus.APPROVED:
                if action.arguments != arguments:
                    return GovernanceVerdict(
                        allowed=False,
                        reason=(
                            "arguments changed since approval; re-approve the "
                            "action as it now stands"
                        ),
                    )
                return GovernanceVerdict(allowed=True, reason="approved by user")

        if spec.risk.value == "external_facing" and (violations := self.egress.check(arguments)):
            log.warning(
                "governance.egress_blocked",
                principal=principal.user_id,
                tool=spec.qualified_name,
                violations=violations,
            )
            return GovernanceVerdict(
                allowed=False,
                reason="; ".join(violations),
            )

        verdict = self.autonomy.decide(principal, spec, tainted=context.tainted)
        if verdict.mode is AutonomyMode.AUTO:
            return GovernanceVerdict(allowed=True, reason=verdict.reason)

        reason = verdict.reason
        if context.tainted and context.taint_summary:
            reason = f"{reason} ({context.taint_summary})"

        action = await self.approvals.submit(principal, spec, arguments, reason=reason)
        return GovernanceVerdict(allowed=False, reason=reason, pending_action_id=action.id)

    async def note_execution(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict,
        result: ToolResult,
    ) -> None:
        """Journal successful mutations so they can be undone."""
        if spec.mutating and result.ok:
            await self.journal.record(principal, spec, arguments, result)

    # -- user-facing operations -------------------------------------------

    async def approve(self, action_id: str, *, note: str | None = None) -> ActionContext:
        """Approve a held action and return the context that lets it execute."""
        action = await self.approvals.decide(action_id, approved=True, note=note)
        return ActionContext(approved_action_id=action.id)

    async def reject(self, action_id: str, *, note: str | None = None) -> None:
        await self.approvals.decide(action_id, approved=False, note=note)

    async def record_decision(
        self, principal: Principal, spec: ToolSpec, *, approved: bool
    ) -> None:
        """Feed a user's decision into the autonomy track record.

        Persisted immediately when the policy supports it, so a crash costs at
        most the decision in flight rather than the user's whole track record.
        """
        if approved:
            self.autonomy.note_approval(principal, spec)
        else:
            self.autonomy.note_rejection(principal, spec)
        if persist := getattr(self.autonomy, "persist", None):
            await persist(principal, spec.qualified_name)
