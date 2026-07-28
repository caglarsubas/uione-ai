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


class SessionRow(Base):
    """A signed-in browser session.

    Durable so a deployment restart does not silently sign everyone out, and so
    logout can revoke rather than merely suggest.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    roles: Mapped[list] = mapped_column(JSON, default=list)

    # Held server-side, never sent to the browser.
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ScheduleRow(Base):
    """One user's recurring brief.

    Durable because losing it is silent: nobody is told their morning brief
    stopped being prepared, they simply stop receiving one and conclude the
    feature does not work.
    """

    __tablename__ = "schedules"

    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    at: Mapped[str] = mapped_column(String(8), default="07:30")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    days: Mapped[list] = mapped_column(JSON, default=list)
    jitter_s: Mapped[int] = mapped_column(Integer, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class DisclosureRow(Base):
    """One person's A2A disclosure policy.

    Losing this reverts everyone to the default, which is *narrower* than most
    people will have configured — so the failure is colleagues' assistants
    quietly refusing questions they used to answer, with no error anywhere.
    """

    __tablename__ = "disclosure_contracts"

    owner_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    default_facets: Mapped[list] = mapped_column(JSON, default=list)
    external_facets: Mapped[list] = mapped_column(JSON, default=list)
    by_role: Mapped[dict] = mapped_column(JSON, default=dict)
    by_user: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DocumentRow(Base):
    """An indexed document and the permissions it came with.

    Only the document is stored. The inverted index is *derived* data and is
    rebuilt at startup — persisting postings would mean a schema change every
    time the tokeniser changes, and a stale index that disagrees with its own
    documents is worse than one that takes a second to rebuild.
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(Text, default="")

    acl_users: Mapped[list] = mapped_column(JSON, default=list)
    acl_groups: Mapped[list] = mapped_column(JSON, default=list)
    acl_denied: Mapped[list] = mapped_column(JSON, default=list)
    acl_visibility: Mapped[str] = mapped_column(String(32), default="restricted")

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    doc_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class SyncWatermarkRow(Base):
    """When each ingestion source was last pulled.

    Durable so a restart does not re-ingest an entire share — and, more
    importantly, so an incremental sync after a restart does not skip the window
    it never actually fetched.
    """

    __tablename__ = "sync_watermarks"

    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_sync: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class McpPinRow(Base):
    """What a third-party MCP server declared when it was approved.

    Durable because the attack it defends against is a *change over time*, and a
    pin that lives in memory approves whatever the server says at each restart —
    which is precisely the moment a rug pull lands.
    """

    __tablename__ = "mcp_pins"

    server: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: Tool name to fingerprint of its description and parameters.
    tools: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    approved_by: Mapped[str] = mapped_column(String(255), default="first-use")
    note: Mapped[str] = mapped_column(Text, default="")


class MetricPointRow(Base):
    """One number, for one person, on one day.

    A daily census rather than a time series database. The volume is a handful
    of rows per person per day, which after a year is still small enough that
    nobody has to think about it — and a product that quietly grows an
    unbounded metrics table on an air-gapped box is one somebody eventually has
    to delete in a hurry.

    Keyed on the *day* rather than the timestamp so a scheduler that fires twice
    — a restart, a retried tick — overwrites rather than double-counting. Two
    entries for one Tuesday would make the detector's weekday baseline quietly
    wrong.
    """

    __tablename__ = "metric_points"

    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    metric: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EmbeddingRow(Base):
    """A cached document vector.

    Stored, unlike the inverted index's postings, and the difference is cost: a
    posting list is a tokeniser pass over text already in memory, while this is
    a round trip to a GPU per document. Re-embedding a corpus on every restart
    would make restarts expensive enough that nobody restarts.

    Keyed by document *and model*, and carrying the content hash, so a changed
    document or a changed embedding model produces a miss rather than a
    confidently wrong vector. Mixing embedding spaces yields similarities that
    are arithmetic on unrelated numbers — plausible, ordered, meaningless.

    The vector is JSON rather than a native array type: this has to work on
    SQLite in an air-gapped install, and a pgvector dependency would make the
    smallest deployment need the largest database.
    """

    __tablename__ = "embeddings"

    document_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list] = mapped_column(JSON, default=list)
    dims: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
