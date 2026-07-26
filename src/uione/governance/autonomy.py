"""Graduated autonomy — gap G1.

No enterprise grants an agent the right to send mail on day one, and no user
trusts it either. Autonomy is therefore *earned*, per user × tool, through a
track record the user can see and revoke:

    preview  →  approve  →  auto

A tool starts held for approval. Each approval the user grants adds to its
record; after enough consecutive approvals with no rejections, that specific
user × tool pair becomes eligible to run unattended. A single rejection resets
the record — the user's "no" is information, and treating it as noise is how an
assistant loses the trust it spent weeks earning.

Three things never auto-run regardless of record:

* irreversible actions (deletions, payments)
* anything while untrusted content is in context (gap G2 — this is the rule that
  actually breaks the lethal trifecta)
* anything a policy explicitly pins to manual
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from uione.mcphub import Principal, RiskClass, ToolSpec

log = structlog.get_logger(__name__)


class AutonomyMode(StrEnum):
    AUTO = "auto"
    """Runs immediately."""

    APPROVE = "approve"
    """Held; the user sees a preview and decides."""

    BLOCKED = "blocked"
    """Refused outright, not offered for approval."""


@dataclass(frozen=True)
class AutonomyVerdict:
    mode: AutonomyMode
    reason: str

    @property
    def may_execute(self) -> bool:
        return self.mode is AutonomyMode.AUTO


@dataclass
class TrackRecord:
    """One user's history with one tool."""

    approvals: int = 0
    rejections: int = 0
    consecutive_approvals: int = 0
    auto_granted: bool = False

    def record_approval(self) -> None:
        self.approvals += 1
        self.consecutive_approvals += 1

    def record_rejection(self) -> None:
        self.rejections += 1
        self.consecutive_approvals = 0
        # Losing earned autonomy on a rejection is the point: the user has just
        # said this assistant's judgement was wrong here.
        self.auto_granted = False


@dataclass
class AutonomyPolicy:
    """Decides how much independence a given action gets."""

    #: Consecutive approvals before a user × tool pair may run unattended.
    promotion_threshold: int = 5

    #: Risk classes that may ever be promoted to auto.
    promotable: frozenset[RiskClass] = frozenset({RiskClass.REVERSIBLE_WRITE})

    #: Fully-qualified tool names pinned to manual approval forever.
    always_manual: frozenset[str] = frozenset()

    _records: dict[tuple[str, str], TrackRecord] = field(default_factory=dict)

    def record_for(self, principal: Principal, tool: str) -> TrackRecord:
        return self._records.setdefault((principal.user_id, tool), TrackRecord())

    def decide(
        self,
        principal: Principal,
        spec: ToolSpec,
        *,
        tainted: bool = False,
    ) -> AutonomyVerdict:
        tool = spec.qualified_name

        if not spec.mutating:
            return AutonomyVerdict(AutonomyMode.AUTO, "read-only action")

        if tool in self.always_manual:
            return AutonomyVerdict(AutonomyMode.APPROVE, "tool is pinned to manual approval")

        if spec.risk is RiskClass.IRREVERSIBLE:
            return AutonomyVerdict(
                AutonomyMode.APPROVE, "irreversible actions always require a human"
            )

        if tainted:
            # The trifecta breaker. An attacker may have influenced the model's
            # plan; a person must look before anything leaves or changes.
            return AutonomyVerdict(
                AutonomyMode.APPROVE,
                "untrusted content is in context; a human must approve this action",
            )

        record = self.record_for(principal, tool)
        if record.auto_granted and spec.risk in self.promotable:
            return AutonomyVerdict(
                AutonomyMode.AUTO,
                f"auto-approval earned after {record.approvals} approvals",
            )

        remaining = max(0, self.promotion_threshold - record.consecutive_approvals)
        if spec.risk in self.promotable and remaining:
            reason = f"{remaining} more approval(s) before this can run unattended"
        else:
            reason = f"{spec.risk} actions require approval"
        return AutonomyVerdict(AutonomyMode.APPROVE, reason)

    def note_approval(self, principal: Principal, spec: ToolSpec) -> TrackRecord:
        record = self.record_for(principal, spec.qualified_name)
        record.record_approval()
        if (
            spec.risk in self.promotable
            and spec.qualified_name not in self.always_manual
            and record.consecutive_approvals >= self.promotion_threshold
            and not record.auto_granted
        ):
            record.auto_granted = True
            log.info(
                "governance.autonomy_earned",
                principal=principal.user_id,
                tool=spec.qualified_name,
                approvals=record.approvals,
            )
        return record

    def note_rejection(self, principal: Principal, spec: ToolSpec) -> TrackRecord:
        record = self.record_for(principal, spec.qualified_name)
        had_auto = record.auto_granted
        record.record_rejection()
        if had_auto:
            log.info(
                "governance.autonomy_revoked",
                principal=principal.user_id,
                tool=spec.qualified_name,
            )
        return record

    def revoke(self, principal: Principal, tool: str) -> None:
        """User-initiated revocation, from the transparency page."""
        record = self.record_for(principal, tool)
        record.auto_granted = False
        record.consecutive_approvals = 0

    def describe(self, principal: Principal) -> dict[str, dict[str, object]]:
        """What this user's assistant may do unattended, for the transparency UI."""
        return {
            tool: {
                "auto": record.auto_granted,
                "approvals": record.approvals,
                "rejections": record.rejections,
                "toward_auto": record.consecutive_approvals,
            }
            for (user_id, tool), record in self._records.items()
            if user_id == principal.user_id
        }
