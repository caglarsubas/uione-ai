from __future__ import annotations

import pytest

from uione.governance import (
    ApprovalStatus,
    AutonomyPolicy,
    EgressPolicy,
    Governor,
)
from uione.mcphub import (
    ActionContext,
    AuditLog,
    AuditOutcome,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
    ToolResult,
    ToolSpec,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))

READ_SPEC = ToolSpec(server="mail", tool="search", description="Search", risk=RiskClass.READ)
WRITE_SPEC = ToolSpec(
    server="jira", tool="update", description="Update issue", risk=RiskClass.REVERSIBLE_WRITE
)
SEND_SPEC = ToolSpec(
    server="mail", tool="send", description="Send mail", risk=RiskClass.EXTERNAL_FACING
)
DELETE_SPEC = ToolSpec(
    server="jira", tool="delete", description="Delete issue", risk=RiskClass.IRREVERSIBLE
)


@pytest.fixture
def governor() -> Governor:
    return Governor()


def clean() -> ActionContext:
    return ActionContext()


def tainted() -> ActionContext:
    return ActionContext(tainted=True, taint_summary="untrusted content from inbound email")


# -- the ladder ------------------------------------------------------------


async def test_reads_run_without_approval(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, READ_SPEC, {}, clean())
    assert verdict.allowed


async def test_writes_are_held_on_first_use(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-1"}, clean())

    assert not verdict.allowed
    assert verdict.pending_action_id
    assert await governor.approvals.pending_for(ALICE)


async def test_autonomy_is_earned_after_repeated_approvals(governor: Governor) -> None:
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, WRITE_SPEC, approved=True)

    verdict = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-1"}, clean())

    assert verdict.allowed
    assert "earned" in verdict.reason


async def test_one_rejection_revokes_earned_autonomy(governor: Governor) -> None:
    """The user's 'no' is information; treating it as noise loses their trust."""
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, WRITE_SPEC, approved=True)
    assert (await governor.authorize(ALICE, WRITE_SPEC, {}, clean())).allowed

    await governor.record_decision(ALICE, WRITE_SPEC, approved=False)

    assert not (await governor.authorize(ALICE, WRITE_SPEC, {}, clean())).allowed


async def test_autonomy_is_per_user_and_per_tool(governor: Governor) -> None:
    bob = Principal(user_id="bob", roles=frozenset({"analyst"}))
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, WRITE_SPEC, approved=True)

    assert (await governor.authorize(ALICE, WRITE_SPEC, {}, clean())).allowed
    assert not (await governor.authorize(bob, WRITE_SPEC, {}, clean())).allowed
    assert not (await governor.authorize(ALICE, SEND_SPEC, {}, clean())).allowed


async def test_irreversible_actions_never_earn_autonomy(governor: Governor) -> None:
    for _ in range(50):
        await governor.record_decision(ALICE, DELETE_SPEC, approved=True)

    verdict = await governor.authorize(ALICE, DELETE_SPEC, {}, clean())

    assert not verdict.allowed
    assert "irreversible" in verdict.reason


async def test_pinned_tools_never_earn_autonomy() -> None:
    governor = Governor(
        autonomy=AutonomyPolicy(always_manual=frozenset({"jira.update"}), promotion_threshold=2)
    )
    for _ in range(10):
        await governor.record_decision(ALICE, WRITE_SPEC, approved=True)

    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, clean())

    assert not verdict.allowed
    assert "pinned" in verdict.reason


async def test_progress_toward_autonomy_is_reported(governor: Governor) -> None:
    """The user should see the ladder, not be surprised by it."""
    await governor.record_decision(ALICE, WRITE_SPEC, approved=True)

    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, clean())

    assert "more approval" in verdict.reason


# -- the trifecta breaker --------------------------------------------------


async def test_taint_forces_approval_despite_earned_autonomy(governor: Governor) -> None:
    """The rule that actually breaks the lethal trifecta."""
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, WRITE_SPEC, approved=True)
    assert (await governor.authorize(ALICE, WRITE_SPEC, {}, clean())).allowed

    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, tainted())

    assert not verdict.allowed
    assert "untrusted content" in verdict.reason


async def test_taint_reason_names_the_source(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, tainted())
    assert "inbound email" in verdict.reason


async def test_taint_does_not_gate_reads(governor: Governor) -> None:
    """Reading more while tainted is fine; it is acting that needs a human."""
    assert (await governor.authorize(ALICE, READ_SPEC, {}, tainted())).allowed


# -- egress ----------------------------------------------------------------


async def test_disallowed_recipient_is_refused_not_offered() -> None:
    """Do not hand the user a one-click button to do what policy forbids."""
    governor = Governor(egress=EgressPolicy(internal_domains=frozenset({"corp.example"})))

    verdict = await governor.authorize(ALICE, SEND_SPEC, {"to": "collector@evil.example"}, clean())

    assert not verdict.allowed
    assert verdict.pending_action_id is None
    assert "evil.example" in verdict.reason


async def test_internal_recipient_still_needs_approval() -> None:
    governor = Governor(egress=EgressPolicy(internal_domains=frozenset({"corp.example"})))

    verdict = await governor.authorize(ALICE, SEND_SPEC, {"to": "cfo@corp.example"}, clean())

    assert not verdict.allowed
    assert verdict.pending_action_id is not None


# -- approval round trip ---------------------------------------------------


async def test_approved_action_executes(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-1"}, clean())

    context = await governor.approve(verdict.pending_action_id)
    second = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-1"}, context)

    assert second.allowed
    assert "approved by user" in second.reason


async def test_arguments_cannot_change_after_approval(governor: Governor) -> None:
    """Approving a preview must not authorise a different payload."""
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-1"}, clean())
    context = await governor.approve(verdict.pending_action_id)

    swapped = await governor.authorize(ALICE, WRITE_SPEC, {"issue": "A-999"}, context)

    assert not swapped.allowed
    assert "changed since approval" in swapped.reason


async def test_preview_is_built_from_real_arguments(governor: Governor) -> None:
    verdict = await governor.authorize(
        ALICE, SEND_SPEC, {"to": "cfo@corp", "body": "hello"}, clean()
    )

    action = await governor.approvals.get(verdict.pending_action_id)

    assert "to: cfo@corp" in action.preview
    assert "body: hello" in action.preview


async def test_rejecting_closes_the_action(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, clean())

    await governor.reject(verdict.pending_action_id, note="wrong ticket")

    action = await governor.approvals.get(verdict.pending_action_id)
    assert action.status is ApprovalStatus.REJECTED
    assert action.note == "wrong ticket"
    assert await governor.approvals.pending_for(ALICE) == []


async def test_deciding_twice_is_refused(governor: Governor) -> None:
    verdict = await governor.authorize(ALICE, WRITE_SPEC, {}, clean())
    await governor.approve(verdict.pending_action_id)

    with pytest.raises(ValueError, match="already"):
        await governor.reject(verdict.pending_action_id)


# -- undo journal ----------------------------------------------------------


async def test_successful_mutations_are_journalled(governor: Governor) -> None:
    await governor.note_execution(ALICE, WRITE_SPEC, {"issue": "A-1"}, ToolResult.success("ok"))

    entries = await governor.journal.recent_for(ALICE)
    assert len(entries) == 1
    assert entries[0].tool == "jira.update"


async def test_reads_are_not_journalled(governor: Governor) -> None:
    await governor.note_execution(ALICE, READ_SPEC, {}, ToolResult.success("ok"))
    assert governor.journal.entries == ()


async def test_failed_mutations_are_not_journalled(governor: Governor) -> None:
    """Nothing changed, so there is nothing to undo."""
    await governor.note_execution(ALICE, WRITE_SPEC, {}, ToolResult.failure("nope"))
    assert governor.journal.entries == ()


async def test_registered_undo_makes_an_action_reversible(governor: Governor) -> None:
    governor.journal.register_undo(
        "jira.update",
        lambda args, _result: ("jira.update", {"issue": args["issue"], "status": "reopened"}),
    )

    await governor.note_execution(
        ALICE, WRITE_SPEC, {"issue": "A-1", "status": "closed"}, ToolResult.success("ok")
    )

    entry = (await governor.journal.recent_for(ALICE))[0]
    assert entry.reversible
    assert entry.undo_arguments == {"issue": "A-1", "status": "reopened"}


async def test_tools_without_an_undo_are_not_claimed_reversible(governor: Governor) -> None:
    await governor.note_execution(ALICE, WRITE_SPEC, {"issue": "A-1"}, ToolResult.success("ok"))
    assert not (await governor.journal.recent_for(ALICE))[0].reversible


async def test_a_broken_undo_builder_does_not_fail_the_action(governor: Governor) -> None:
    def explode(_args, _result):
        raise RuntimeError("bad builder")

    governor.journal.register_undo("jira.update", explode)

    await governor.note_execution(ALICE, WRITE_SPEC, {"issue": "A-1"}, ToolResult.success("ok"))

    assert len(governor.journal.entries) == 1


# -- integration with the gateway -----------------------------------------


async def build_governed_gateway(governor: Governor) -> tuple[McpGateway, InMemoryAuditSink, list]:
    executed: list[dict] = []
    source = InMemoryToolSource("jira")

    async def update(args: dict) -> ToolResult:
        executed.append(args)
        return ToolResult.success("updated")

    source.register("update", update, description="Update issue", risk=RiskClass.REVERSIBLE_WRITE)
    sink = InMemoryAuditSink()
    gateway = McpGateway(
        policy=ToolPolicy([Grant(role="analyst", tools=frozenset({"jira.update"}))]),
        audit=AuditLog(sink),
        governor=governor,
    )
    await gateway.register(source)
    return gateway, sink, executed


async def test_held_action_never_reaches_the_connector(governor: Governor) -> None:
    gateway, sink, executed = await build_governed_gateway(governor)

    call = await gateway.call(ALICE, "jira.update", {"issue": "A-1"})

    assert not call.ok
    assert call.held
    assert executed == []
    assert sink.records[0].outcome is AuditOutcome.HELD_FOR_APPROVAL


async def test_held_action_is_explained_to_the_model(governor: Governor) -> None:
    gateway, _, _ = await build_governed_gateway(governor)

    call = await gateway.call(ALICE, "jira.update", {"issue": "A-1"})

    assert "needs your approval" in (call.result.error or "")


async def test_approved_action_then_executes(governor: Governor) -> None:
    gateway, sink, executed = await build_governed_gateway(governor)
    held = await gateway.call(ALICE, "jira.update", {"issue": "A-1"})

    context = await governor.approve(held.pending_action_id)
    done = await gateway.call(ALICE, "jira.update", {"issue": "A-1"}, context=context)

    assert done.ok
    assert executed == [{"issue": "A-1"}]
    assert sink.records[-1].outcome is AuditOutcome.ALLOWED


async def test_earned_autonomy_executes_straight_through(governor: Governor) -> None:
    gateway, _, executed = await build_governed_gateway(governor)
    spec = gateway.spec("jira.update")
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, spec, approved=True)

    call = await gateway.call(ALICE, "jira.update", {"issue": "A-2"})

    assert call.ok
    assert executed == [{"issue": "A-2"}]


async def test_tainted_context_holds_even_with_earned_autonomy(governor: Governor) -> None:
    gateway, _, executed = await build_governed_gateway(governor)
    spec = gateway.spec("jira.update")
    for _ in range(governor.autonomy.promotion_threshold):
        await governor.record_decision(ALICE, spec, approved=True)

    call = await gateway.call(ALICE, "jira.update", {"issue": "A-3"}, context=tainted())

    assert call.held
    assert executed == []
