"""The audit tap.

Every tool call passes through here — allowed, denied, failed, or rate-limited.
A call that reaches a connector without an audit record is a defect, not an
optimisation, so the gateway is written so that no code path can skip it.

Arguments are recorded as a hash by default. Enterprise tool arguments routinely
contain personal data (mail addresses, customer identifiers, claim details), and
an audit log that is itself a PII spill helps nobody. Deployments that need full
argument capture for forensics opt in explicitly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

from uione.mcphub.types import Principal, RiskClass

log = structlog.get_logger("uione.audit")


class AuditOutcome(StrEnum):
    ALLOWED = "allowed"
    """Executed and returned successfully."""

    DENIED = "denied"
    """Blocked by policy before reaching the connector."""

    RATE_LIMITED = "rate_limited"
    UNKNOWN_TOOL = "unknown_tool"
    CIRCUIT_OPEN = "circuit_open"

    HELD_FOR_APPROVAL = "held_for_approval"
    """Governance withheld execution pending a human decision."""

    FAILED = "failed"
    """Reached the connector, which errored."""

    UNCONFIRMED = "unconfirmed"
    """Executed, and reading it back does not show the effect it claimed.

    Distinct from ``FAILED`` on purpose. The call succeeded — something may well
    have changed — so an auditor asking "what did this assistant actually do"
    must not see these filed alongside calls that never took effect.
    """


class AuditRecord(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_id: str
    tool: str
    server: str
    risk: RiskClass
    outcome: AuditOutcome
    arguments_hash: str
    arguments: dict[str, Any] | None = None
    duration_ms: float = 0.0
    detail: str | None = None
    correlation_id: str | None = None

    verification: str | None = None
    """Read-after-write verdict, when one was reached. See F2.6.

    A string rather than the enum so a record deserialised from an older store,
    or from a deployment with verification off, stays readable.
    """

    @property
    def verified(self) -> bool:
        """The north-star metric counts these, and only these.

        Deliberately not "did not report a problem": an action with no registered
        read-back is unverified, and counting it as verified would make the
        headline number grow by adding tools nobody checked.
        """
        return self.verification == "confirmed"

    @property
    def mutating_and_succeeded(self) -> bool:
        return self.outcome is AuditOutcome.ALLOWED and self.risk in {
            RiskClass.REVERSIBLE_WRITE,
            RiskClass.IRREVERSIBLE,
            RiskClass.EXTERNAL_FACING,
        }


def hash_arguments(arguments: dict[str, Any]) -> str:
    """Stable hash of tool arguments.

    Sorted keys so the same call hashes identically across runs, which is what
    makes "this exact call happened before" answerable without storing the data.
    """
    canonical = json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class AuditSink(Protocol):
    """Where audit records go. Production ships them to the customer's SIEM."""

    async def write(self, record: AuditRecord) -> None: ...


class InMemoryAuditSink:
    """Append-only in-process sink. Used in tests and single-node PoCs."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> Sequence[AuditRecord]:
        return tuple(self._records)

    def for_principal(self, user_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.principal_id == user_id]

    def with_outcome(self, outcome: AuditOutcome) -> list[AuditRecord]:
        return [r for r in self._records if r.outcome is outcome]


class StructlogAuditSink:
    """Emits audit records to the structured log stream for shipping to a SIEM."""

    async def write(self, record: AuditRecord) -> None:
        log.info(
            "tool_call",
            principal=record.principal_id,
            tool=record.tool,
            server=record.server,
            risk=str(record.risk),
            outcome=str(record.outcome),
            args_hash=record.arguments_hash,
            duration_ms=round(record.duration_ms, 2),
            detail=record.detail,
            correlation_id=record.correlation_id,
            verification=record.verification,
        )


class FanOutAuditSink:
    """Writes to several sinks.

    A failing sink must not stop the others, and must not fail the tool call it
    is describing — losing one copy of a record is bad, dropping the user's work
    because a log shipper is down is worse.
    """

    def __init__(self, *sinks: AuditSink) -> None:
        self._sinks = sinks

    async def write(self, record: AuditRecord) -> None:
        for sink in self._sinks:
            try:
                await sink.write(record)
            except Exception:  # noqa: BLE001
                log.exception("audit.sink_failed", sink=type(sink).__name__)


class AuditLog:
    """Front door to auditing, holding the redaction policy."""

    def __init__(self, sink: AuditSink | None = None, *, record_arguments: bool = False) -> None:
        self._sink = sink or StructlogAuditSink()
        self._record_arguments = record_arguments

    async def record(
        self,
        *,
        principal: Principal,
        server: str,
        tool: str,
        risk: RiskClass,
        outcome: AuditOutcome,
        arguments: dict[str, Any],
        duration_ms: float = 0.0,
        detail: str | None = None,
        correlation_id: str | None = None,
        verification: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            principal_id=principal.user_id,
            server=server,
            tool=tool,
            risk=risk,
            outcome=outcome,
            arguments_hash=hash_arguments(arguments),
            arguments=arguments if self._record_arguments else None,
            duration_ms=duration_ms,
            detail=detail,
            correlation_id=correlation_id,
            verification=verification,
        )
        await self._sink.write(record)
        return record
