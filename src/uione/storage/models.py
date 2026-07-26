"""Database schema.

SQLite by default so a fresh checkout needs no infrastructure; PostgreSQL in
production through the same code path, selected by ``UIONE_DATABASE_URL``.

The audit table is append-only by convention *and* by the absence of any update
or delete path in the repository above it. It is not the only copy of the record
— production ships audit events to the customer's SIEM as well — but a governed
product whose local trail vanishes on restart is not credible.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AuditRow(Base):
    """One tool call, however it ended."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    server: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(255), index=True)
    risk: Mapped[str] = mapped_column(String(32), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


# The two questions auditors actually ask: "what did this person do?" and "what
# irreversible things happened last week?". Both are time-ordered.
Index("ix_audit_principal_time", AuditRow.principal_id, AuditRow.timestamp)
Index("ix_audit_risk_time", AuditRow.risk, AuditRow.timestamp)


class PendingActionRow(Base):
    """A mutating action awaiting a human decision."""

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    tool: Mapped[str] = mapped_column(String(255))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class JournalRow(Base):
    """A completed mutating action and how to reverse it."""

    __tablename__ = "action_journal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    tool: Mapped[str] = mapped_column(String(255))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    undo_tool: Mapped[str | None] = mapped_column(String(255), nullable=True)
    undo_arguments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)


class AutonomyRow(Base):
    """One user's earned track record with one tool.

    Durable because it is the user's own history: losing it on a restart would
    silently demote every tool back to manual approval and make the ladder feel
    arbitrary, which is exactly how an approval flow becomes noise people
    click through.
    """

    __tablename__ = "autonomy_records"

    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tool: Mapped[str] = mapped_column(String(255), primary_key=True)
    approvals: Mapped[int] = mapped_column(Integer, default=0)
    rejections: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_approvals: Mapped[int] = mapped_column(Integer, default=0)
    auto_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
