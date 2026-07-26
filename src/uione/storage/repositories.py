"""Durable implementations of the governance stores.

Each class satisfies the interface its in-memory counterpart already defined, so
swapping them is a wiring change rather than a refactor. That was the point of
defining those interfaces before there was a database behind them.

One deliberate asymmetry: reads that serve a request go to the database, but the
autonomy ladder keeps an in-memory cache written through to storage. Autonomy is
consulted on *every* mutating call, and a database round-trip inside the
governance check would put storage latency on the critical path of every action.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from uione.governance.approvals import ApprovalStatus, JournalEntry, PendingAction, render_preview
from uione.governance.autonomy import AutonomyPolicy, TrackRecord
from uione.mcphub import Principal, RiskClass, ToolSpec
from uione.mcphub.audit import AuditRecord
from uione.storage.database import Database
from uione.storage.models import AuditRow, AutonomyRow, JournalRow, PendingActionRow

log = structlog.get_logger(__name__)


class SqlAuditSink:
    """Append-only audit sink.

    Exposes no update or delete path. An audit trail with an edit method is a
    trail an auditor has to take on trust.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def write(self, record: AuditRecord) -> None:
        async with self._db.session() as session:
            session.add(
                AuditRow(
                    timestamp=record.timestamp,
                    principal_id=record.principal_id,
                    server=record.server,
                    tool=record.tool,
                    risk=str(record.risk),
                    outcome=str(record.outcome),
                    arguments_hash=record.arguments_hash,
                    arguments=record.arguments,
                    duration_ms=record.duration_ms,
                    detail=record.detail,
                    correlation_id=record.correlation_id,
                )
            )

    async def recent(
        self,
        *,
        principal_id: str | None = None,
        risk: RiskClass | None = None,
        limit: int = 100,
    ) -> list[AuditRow]:
        stmt = select(AuditRow).order_by(AuditRow.timestamp.desc()).limit(limit)
        if principal_id:
            stmt = stmt.where(AuditRow.principal_id == principal_id)
        if risk:
            stmt = stmt.where(AuditRow.risk == str(risk))
        async with self._db.session() as session:
            return list((await session.execute(stmt)).scalars())

    async def count(self) -> int:
        async with self._db.session() as session:
            rows = await session.execute(select(AuditRow.id))
            return len(list(rows.scalars()))


class SqlApprovalStore:
    """Approval queue that survives a restart.

    In-memory, a deployment restart silently discards every action a user was
    about to decide on. They are not told; the actions simply stop existing,
    which is indistinguishable from the assistant having done them.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    async def submit(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict,
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
        async with self._db.session() as session:
            session.add(
                PendingActionRow(
                    id=action.id,
                    principal_id=action.principal_id,
                    tool=action.tool,
                    arguments=action.arguments,
                    risk=str(action.risk),
                    reason=action.reason,
                    preview=action.preview,
                    status=str(action.status),
                    created_at=action.created_at,
                )
            )
        log.info("governance.action_held", action_id=action.id, tool=action.tool)
        return action

    async def get(self, action_id: str) -> PendingAction | None:
        async with self._db.session() as session:
            row = await session.get(PendingActionRow, action_id)
            return _to_action(row) if row else None

    async def pending_for(self, principal: Principal) -> list[PendingAction]:
        stmt = (
            select(PendingActionRow)
            .where(PendingActionRow.principal_id == principal.user_id)
            .where(PendingActionRow.status == str(ApprovalStatus.PENDING))
            .order_by(PendingActionRow.created_at)
        )
        async with self._db.session() as session:
            return [_to_action(r) for r in (await session.execute(stmt)).scalars()]

    async def decide(
        self, action_id: str, *, approved: bool, note: str | None = None
    ) -> PendingAction:
        async with self._db.session() as session:
            row = await session.get(PendingActionRow, action_id)
            if row is None:
                raise KeyError(f"no such pending action: {action_id}")
            if row.status != str(ApprovalStatus.PENDING):
                raise ValueError(f"action {action_id} is already {row.status}")
            row.status = str(ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED)
            row.decided_at = datetime.now(UTC)
            row.note = note
            return _to_action(row)


class SqlActionJournal:
    """Undo journal backed by storage."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._undo_builders: dict = {}

    def register_undo(self, tool: str, builder) -> None:
        self._undo_builders[tool] = builder

    async def record(
        self, principal: Principal, spec: ToolSpec, arguments: dict, result=None
    ) -> JournalEntry:
        undo_tool = None
        undo_arguments = None
        if builder := self._undo_builders.get(spec.qualified_name):
            try:
                if built := builder(arguments, result):
                    undo_tool, undo_arguments = built
            except Exception:  # noqa: BLE001 — a bad builder must not fail the action
                log.exception("governance.undo_builder_failed", tool=spec.qualified_name)

        entry = JournalEntry(
            id=uuid.uuid4().hex[:12],
            principal_id=principal.user_id,
            tool=spec.qualified_name,
            arguments=dict(arguments),
            risk=spec.risk,
            undo_tool=undo_tool,
            undo_arguments=undo_arguments,
        )
        async with self._db.session() as session:
            session.add(
                JournalRow(
                    id=entry.id,
                    principal_id=entry.principal_id,
                    tool=entry.tool,
                    arguments=entry.arguments,
                    risk=str(entry.risk),
                    at=entry.at,
                    undo_tool=entry.undo_tool,
                    undo_arguments=entry.undo_arguments,
                )
            )
        return entry

    async def recent_for(self, principal: Principal, limit: int = 20) -> list[JournalEntry]:
        stmt = (
            select(JournalRow)
            .where(JournalRow.principal_id == principal.user_id)
            .order_by(JournalRow.at.desc())
            .limit(limit)
        )
        async with self._db.session() as session:
            return [_to_entry(r) for r in (await session.execute(stmt)).scalars()]

    async def mark_undone(self, entry_id: str) -> None:
        async with self._db.session() as session:
            if row := await session.get(JournalRow, entry_id):
                row.undone = True


class PersistentAutonomyPolicy(AutonomyPolicy):
    """Autonomy ladder that remembers across restarts.

    Reads come from an in-memory cache loaded at startup, because
    :meth:`decide` runs on every mutating call and storage latency does not
    belong on that path. Writes go through to the database immediately, so a
    crash loses at most the decision currently in flight.
    """

    def __init__(self, database: Database, **kwargs) -> None:
        super().__init__(**kwargs)
        self._db = database

    async def load(self) -> int:
        async with self._db.session() as session:
            rows = list((await session.execute(select(AutonomyRow))).scalars())
        for row in rows:
            self._records[(row.principal_id, row.tool)] = TrackRecord(
                approvals=row.approvals,
                rejections=row.rejections,
                consecutive_approvals=row.consecutive_approvals,
                auto_granted=row.auto_granted,
            )
        log.info("storage.autonomy_loaded", records=len(rows))
        return len(rows)

    async def persist(self, principal: Principal, tool: str) -> None:
        record = self.record_for(principal, tool)
        async with self._db.session() as session:
            row = await session.get(AutonomyRow, (principal.user_id, tool))
            if row is None:
                row = AutonomyRow(principal_id=principal.user_id, tool=tool)
                session.add(row)
            row.approvals = record.approvals
            row.rejections = record.rejections
            row.consecutive_approvals = record.consecutive_approvals
            row.auto_granted = record.auto_granted
            row.updated_at = datetime.now(UTC)


def _to_action(row: PendingActionRow) -> PendingAction:
    return PendingAction(
        id=row.id,
        principal_id=row.principal_id,
        tool=row.tool,
        arguments=dict(row.arguments or {}),
        risk=RiskClass(row.risk),
        reason=row.reason,
        preview=row.preview,
        created_at=row.created_at,
        status=ApprovalStatus(row.status),
        decided_at=row.decided_at,
        note=row.note,
    )


def _to_entry(row: JournalRow) -> JournalEntry:
    return JournalEntry(
        id=row.id,
        principal_id=row.principal_id,
        tool=row.tool,
        arguments=dict(row.arguments or {}),
        risk=RiskClass(row.risk),
        at=row.at,
        undo_tool=row.undo_tool,
        undo_arguments=row.undo_arguments,
        undone=row.undone,
    )
