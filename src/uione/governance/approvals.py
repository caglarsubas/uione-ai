"""Pending actions and the undo journal.

The approval queue is where held actions wait for a human. The journal is what
makes saying yes feel safe: every mutating action records how to undo it, so the
user's mental model shifts from "this might be irreversible" to "I can put that
back" (gap G13). That shift is what makes the approval ladder tolerable rather
than exhausting.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from uione.mcphub import Principal, RiskClass, ToolSpec

log = structlog.get_logger(__name__)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class PendingAction:
    """A mutating action awaiting a decision."""

    id: str
    principal_id: str
    tool: str
    arguments: dict[str, Any]
    risk: RiskClass
    reason: str
    preview: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    note: str | None = None

    @property
    def open(self) -> bool:
        return self.status is ApprovalStatus.PENDING


def render_preview(spec: ToolSpec, arguments: dict[str, Any]) -> str:
    """Human-readable description of what is about to happen.

    Shown in the approval card. Deliberately built from the *repaired* arguments
    that would actually be sent, not from the model's narration of its intent —
    approving a summary that differs from the payload is the failure this guards
    against.
    """
    if not arguments:
        return f"{spec.qualified_name} (no arguments)"
    lines = [f"{spec.qualified_name} — {spec.description or 'no description'}"]
    for key, value in sorted(arguments.items()):
        rendered = str(value)
        if len(rendered) > 300:
            rendered = rendered[:300] + f"… (+{len(rendered) - 300} chars)"
        lines.append(f"  {key}: {rendered}")
    return "\n".join(lines)


class ApprovalStore:
    """In-memory approval queue.

    Async despite needing no I/O, so that it and the SQL-backed store present
    exactly the same interface. A sync/async split here would force every caller
    to know which implementation it holds, which is the coupling the interface
    exists to prevent.
    """

    def __init__(self) -> None:
        self._actions: dict[str, PendingAction] = {}

    async def submit(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict[str, Any],
        *,
        reason: str,
    ) -> PendingAction:
        action = PendingAction(
            id=uuid.uuid4().hex[:12],
            principal_id=principal.user_id,
            tool=spec.qualified_name,
            arguments=dict(arguments),
            risk=spec.risk,
            reason=reason,
            preview=render_preview(spec, arguments),
        )
        self._actions[action.id] = action
        log.info(
            "governance.action_held",
            action_id=action.id,
            principal=principal.user_id,
            tool=action.tool,
            risk=str(action.risk),
        )
        return action

    async def get(self, action_id: str) -> PendingAction | None:
        return self._actions.get(action_id)

    async def pending_for(self, principal: Principal) -> list[PendingAction]:
        return [a for a in self._actions.values() if a.principal_id == principal.user_id and a.open]

    async def pending_count(self) -> int:
        """Open actions across everyone.

        Aggregate on purpose — it feeds a metrics gauge, and a per-user
        breakdown there would be a surveillance surface (G15). A growing backlog
        is an operational signal; whose backlog it is belongs to the audit log.
        """
        return sum(1 for a in self._actions.values() if a.open)

    async def decide(
        self, action_id: str, *, approved: bool, note: str | None = None
    ) -> PendingAction:
        action = self._actions.get(action_id)
        if action is None:
            raise KeyError(f"no such pending action: {action_id}")
        if not action.open:
            raise ValueError(f"action {action_id} is already {action.status}")
        action.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        action.decided_at = datetime.now(UTC)
        action.note = note
        return action

    def mark(self, action_id: str, status: ApprovalStatus) -> None:
        if action := self._actions.get(action_id):
            action.status = status

    @property
    def all_actions(self) -> Sequence[PendingAction]:
        return tuple(self._actions.values())


@dataclass
class JournalEntry:
    """A completed mutating action and how to reverse it."""

    id: str
    principal_id: str
    tool: str
    arguments: dict[str, Any]
    risk: RiskClass
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    undo_tool: str | None = None
    undo_arguments: dict[str, Any] | None = None
    undone: bool = False

    @property
    def reversible(self) -> bool:
        return self.undo_tool is not None and not self.undone


#: How to reverse a write, keyed by the tool that performed it. Connectors
#: register their own; a tool absent from this map is treated as irreversible,
#: which is the safe direction to be wrong in.
UndoBuilder = Any


class ActionJournal:
    """Records mutating actions and builds their compensating actions."""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._undo_builders: dict[str, Any] = {}

    def register_undo(self, tool: str, builder: Any) -> None:
        """Register how to reverse ``tool``.

        ``builder(arguments, result) -> (undo_tool, undo_arguments) | None``
        """
        self._undo_builders[tool] = builder

    async def record(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict[str, Any],
        result: Any = None,
    ) -> JournalEntry:
        undo_tool: str | None = None
        undo_arguments: dict[str, Any] | None = None

        if builder := self._undo_builders.get(spec.qualified_name):
            try:
                built = builder(arguments, result)
            except Exception:  # noqa: BLE001 — a bad builder must not fail the action
                log.exception("governance.undo_builder_failed", tool=spec.qualified_name)
            else:
                if built:
                    undo_tool, undo_arguments = built

        entry = JournalEntry(
            id=uuid.uuid4().hex[:12],
            principal_id=principal.user_id,
            tool=spec.qualified_name,
            arguments=dict(arguments),
            risk=spec.risk,
            undo_tool=undo_tool,
            undo_arguments=undo_arguments,
        )
        self._entries.append(entry)
        return entry

    def get(self, entry_id: str) -> JournalEntry | None:
        return next((e for e in self._entries if e.id == entry_id), None)

    async def recent_for(self, principal: Principal, limit: int = 20) -> list[JournalEntry]:
        entries = [e for e in self._entries if e.principal_id == principal.user_id]
        return sorted(entries, key=lambda e: e.at, reverse=True)[:limit]

    async def undoable_for(self, principal: Principal) -> list[JournalEntry]:
        return [e for e in await self.recent_for(principal) if e.reversible]

    async def mark_undone(self, entry_id: str) -> None:
        if entry := self.get(entry_id):
            entry.undone = True

    @property
    def entries(self) -> Sequence[JournalEntry]:
        return tuple(self._entries)
