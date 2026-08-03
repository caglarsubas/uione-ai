"""Durability tests.

The central assertion in every case below is the same: build a store, write to
it, throw it away, build a *new* one over the same database, and find the state
still there. That is the only thing "durable" means, and it is the only way to
catch a store that quietly keeps everything in a Python dict.
"""

from __future__ import annotations

import pytest

from uione.config import Settings
from uione.governance import ApprovalStatus, Governor
from uione.governance.autonomy import AutonomyMode
from uione.mcphub import ActionContext, AuditLog, AuditOutcome, Principal, RiskClass, ToolSpec
from uione.mcphub.types import ToolResult
from uione.storage import (
    Database,
    PersistentAutonomyPolicy,
    SqlActionJournal,
    SqlApprovalStore,
    SqlAuditSink,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"analyst"}))

WRITE_SPEC = ToolSpec(
    server="jira", tool="update", description="Update issue", risk=RiskClass.REVERSIBLE_WRITE
)
READ_SPEC = ToolSpec(server="mail", tool="search", description="Search", risk=RiskClass.READ)


@pytest.fixture
async def db(tmp_path):
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'g.db'}"))
    await database.create_schema()
    yield database
    await database.dispose()


def reopen(tmp_path) -> Database:
    """A fresh Database over the same file — the restart being simulated."""
    return Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'g.db'}"))


# -- audit -----------------------------------------------------------------


async def test_audit_records_survive_a_restart(db: Database, tmp_path) -> None:
    log = AuditLog(SqlAuditSink(db))
    await log.record(
        principal=ALICE,
        server="mail",
        tool="mail.send_reply",
        risk=RiskClass.EXTERNAL_FACING,
        outcome=AuditOutcome.ALLOWED,
        arguments={"to": "cfo@corp.example"},
    )

    second = reopen(tmp_path)
    try:
        rows = await SqlAuditSink(second).recent()
    finally:
        await second.dispose()

    assert len(rows) == 1
    assert rows[0].tool == "mail.send_reply"
    assert rows[0].outcome == "allowed"


async def test_audit_hashes_arguments_by_default(db: Database) -> None:
    sink = SqlAuditSink(db)
    await AuditLog(sink).record(
        principal=ALICE,
        server="mail",
        tool="mail.search",
        risk=RiskClass.READ,
        outcome=AuditOutcome.ALLOWED,
        arguments={"query": "patient records"},
    )

    row = (await sink.recent())[0]

    assert row.arguments is None
    assert len(row.arguments_hash) == 32


async def test_audit_is_filterable_by_risk(db: Database) -> None:
    """'What irreversible things happened?' must not require reading everything."""
    log = AuditLog(SqlAuditSink(db))
    for risk in (RiskClass.READ, RiskClass.IRREVERSIBLE, RiskClass.READ):
        await log.record(
            principal=ALICE,
            server="s",
            tool="s.t",
            risk=risk,
            outcome=AuditOutcome.ALLOWED,
            arguments={},
        )

    rows = await SqlAuditSink(db).recent(risk=RiskClass.IRREVERSIBLE)

    assert len(rows) == 1


async def test_audit_is_filterable_by_principal(db: Database) -> None:
    log = AuditLog(SqlAuditSink(db))
    for principal in (ALICE, BOB, ALICE):
        await log.record(
            principal=principal,
            server="s",
            tool="s.t",
            risk=RiskClass.READ,
            outcome=AuditOutcome.ALLOWED,
            arguments={},
        )

    assert len(await SqlAuditSink(db).recent(principal_id="alice")) == 2


async def test_audit_sink_exposes_no_mutation_path() -> None:
    """An audit trail with an edit method is one an auditor must take on trust."""
    forbidden = {"update", "delete", "edit", "remove", "purge", "clear"}
    assert not forbidden & {m for m in dir(SqlAuditSink) if not m.startswith("_")}


# -- approvals -------------------------------------------------------------


async def test_pending_approvals_survive_a_restart(db: Database, tmp_path) -> None:
    """Otherwise a restart silently discards what the user was about to decide."""
    action = await SqlApprovalStore(db).submit(
        ALICE, WRITE_SPEC, {"issue": "PAY-1"}, reason="first use"
    )

    second = reopen(tmp_path)
    try:
        pending = await SqlApprovalStore(second).pending_for(ALICE)
    finally:
        await second.dispose()

    assert [p.id for p in pending] == [action.id]
    assert pending[0].arguments == {"issue": "PAY-1"}
    assert "PAY-1" in pending[0].preview


async def test_decided_actions_leave_the_queue(db: Database) -> None:
    store = SqlApprovalStore(db)
    action = await store.submit(ALICE, WRITE_SPEC, {}, reason="r")

    await store.decide(action.id, approved=True)

    assert await store.pending_for(ALICE) == []
    assert (await store.get(action.id)).status is ApprovalStatus.APPROVED


async def test_deciding_twice_is_refused(db: Database) -> None:
    store = SqlApprovalStore(db)
    action = await store.submit(ALICE, WRITE_SPEC, {}, reason="r")
    await store.decide(action.id, approved=True)

    with pytest.raises(ValueError, match="already"):
        await store.decide(action.id, approved=False)


async def test_unknown_action_raises(db: Database) -> None:
    with pytest.raises(KeyError):
        await SqlApprovalStore(db).decide("nope", approved=True)


async def test_queues_are_per_principal(db: Database) -> None:
    store = SqlApprovalStore(db)
    await store.submit(ALICE, WRITE_SPEC, {}, reason="r")

    assert await store.pending_for(BOB) == []


# -- journal ---------------------------------------------------------------


async def test_journal_entries_survive_a_restart(db: Database, tmp_path) -> None:
    journal = SqlActionJournal(db)
    journal.register_undo(
        "jira.update", lambda args, _r: ("jira.update", {"issue": args["issue"], "status": "open"})
    )
    await journal.record(ALICE, WRITE_SPEC, {"issue": "PAY-1", "status": "done"})

    second = reopen(tmp_path)
    try:
        entries = await SqlActionJournal(second).recent_for(ALICE)
    finally:
        await second.dispose()

    assert len(entries) == 1
    assert entries[0].reversible
    assert entries[0].undo_arguments == {"issue": "PAY-1", "status": "open"}


async def test_actions_without_an_undo_are_not_claimed_reversible(db: Database) -> None:
    journal = SqlActionJournal(db)
    await journal.record(ALICE, WRITE_SPEC, {"issue": "PAY-1"})

    assert not (await journal.recent_for(ALICE))[0].reversible


async def test_a_broken_undo_builder_does_not_fail_the_action(db: Database) -> None:
    journal = SqlActionJournal(db)

    def explode(_a, _r):
        raise RuntimeError("bad builder")

    journal.register_undo("jira.update", explode)
    await journal.record(ALICE, WRITE_SPEC, {"issue": "PAY-1"})

    assert len(await journal.recent_for(ALICE)) == 1


# -- autonomy --------------------------------------------------------------


async def test_earned_autonomy_survives_a_restart(db: Database, tmp_path) -> None:
    """Losing this on restart would silently demote every tool back to manual."""
    policy = PersistentAutonomyPolicy(db)
    for _ in range(policy.promotion_threshold):
        policy.note_approval(ALICE, WRITE_SPEC)
        await policy.persist(ALICE, WRITE_SPEC.qualified_name)

    second_db = reopen(tmp_path)
    try:
        restored = PersistentAutonomyPolicy(second_db)
        await restored.load()
        verdict = restored.decide(ALICE, WRITE_SPEC)
    finally:
        await second_db.dispose()

    assert verdict.mode is AutonomyMode.AUTO


async def test_a_rejection_survives_a_restart(db: Database, tmp_path) -> None:
    policy = PersistentAutonomyPolicy(db)
    for _ in range(policy.promotion_threshold):
        policy.note_approval(ALICE, WRITE_SPEC)
    policy.note_rejection(ALICE, WRITE_SPEC)
    await policy.persist(ALICE, WRITE_SPEC.qualified_name)

    second_db = reopen(tmp_path)
    try:
        restored = PersistentAutonomyPolicy(second_db)
        await restored.load()
        verdict = restored.decide(ALICE, WRITE_SPEC)
    finally:
        await second_db.dispose()

    assert verdict.mode is AutonomyMode.APPROVE


async def test_autonomy_is_scoped_per_user_across_restarts(db: Database, tmp_path) -> None:
    policy = PersistentAutonomyPolicy(db)
    for _ in range(policy.promotion_threshold):
        policy.note_approval(ALICE, WRITE_SPEC)
    await policy.persist(ALICE, WRITE_SPEC.qualified_name)

    second_db = reopen(tmp_path)
    try:
        restored = PersistentAutonomyPolicy(second_db)
        await restored.load()
        assert restored.decide(ALICE, WRITE_SPEC).mode is AutonomyMode.AUTO
        assert restored.decide(BOB, WRITE_SPEC).mode is AutonomyMode.APPROVE
    finally:
        await second_db.dispose()


# -- the governor over durable stores -------------------------------------


async def test_governor_works_unchanged_over_sql_stores(db: Database) -> None:
    """The interfaces were defined before there was a database; this proves it."""
    governor = Governor(
        autonomy=PersistentAutonomyPolicy(db),
        approvals=SqlApprovalStore(db),
        journal=SqlActionJournal(db),
    )

    verdict = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "PAY-1"}, ActionContext())
    assert not verdict.allowed

    context = await governor.approve(verdict.pending_action_id)
    second = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "PAY-1"}, context)

    assert second.allowed


async def test_reads_are_not_journalled_over_sql(db: Database) -> None:
    governor = Governor(
        autonomy=PersistentAutonomyPolicy(db),
        approvals=SqlApprovalStore(db),
        journal=SqlActionJournal(db),
    )

    await governor.note_execution(ALICE, READ_SPEC, {}, ToolResult.success("ok"))

    assert await governor.journal.recent_for(ALICE) == []


async def test_credentials_are_stripped_from_logged_urls() -> None:
    from uione.storage.database import _safe_url

    assert _safe_url("postgresql+asyncpg://user:secret@db.corp:5432/uione") == (
        "postgresql+asyncpg://***@db.corp:5432/uione"
    )
    assert _safe_url("sqlite+aiosqlite:///./uione.db") == "sqlite+aiosqlite:///./uione.db"


# -- the two journals must answer the same questions -----------------------


#: What a caller may rely on from *either* journal.
#:
#: Written out rather than derived from the in-memory class, because that class
#: also carries `entries` — a test-only accessor returning the whole list.
#: Implementing that against storage would mean loading an unbounded table to
#: answer a question no production caller asks, so it is deliberately not in the
#: contract and the durable journal deliberately lacks it.
JOURNAL_CONTRACT = frozenset(
    {"record", "register_undo", "get", "recent_for", "undoable_for", "mark_undone"}
)


async def test_both_journals_satisfy_the_journal_contract() -> None:
    """One role, two implementations, and they had drifted.

    `undoable_for` existed on the in-memory journal and not on the durable one.
    Nothing noticed, because nothing had called it through storage — until a UI
    did, and got an AttributeError out of the production path.

    The module docstring for repositories.py opens by claiming "each class
    satisfies the interface its in-memory counterpart already defined". This is
    the test that makes that sentence true rather than aspirational.
    """
    from uione.governance.approvals import ActionJournal
    from uione.storage.repositories import SqlActionJournal

    for journal in (ActionJournal, SqlActionJournal):
        missing = sorted(m for m in JOURNAL_CONTRACT if not hasattr(journal, m))
        assert not missing, f"{journal.__name__} is missing {missing}"
