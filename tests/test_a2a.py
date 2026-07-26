"""A2A tests.

The scenario driving all of it: Alice's assistant wants a meeting with Bob. It
should learn *when Bob is free* and nothing else — not what his meetings are
about, not what he is working on — and Bob personally decides whether the meeting
happens.
"""

from __future__ import annotations

import pytest

from uione.a2a import (
    DEFAULT_TEAM,
    A2ABus,
    A2ARequest,
    AgentCard,
    AgentDirectory,
    Capability,
    ContractRegistry,
    DisclosureContract,
    Facet,
    Outcome,
    RequestKind,
)
from uione.governance import ApprovalStore
from uione.mcphub import AuditLog, AuditOutcome, InMemoryAuditSink, Principal

ALL_CAPS = frozenset(Capability)


def card(owner: str, *, organisation: str = "internal", caps=ALL_CAPS) -> AgentCard:
    return AgentCard(
        agent_id=f"agent:{owner}",
        owner_id=owner,
        display_name=f"{owner.title()}'s assistant",
        capabilities=caps,
        organisation=organisation,
    )


async def answerer(target: AgentCard, request: A2ARequest, granted: frozenset[Facet]) -> dict:
    """A fixture assistant.

    Note the shape: it is *handed* the facets it may reveal, so it cannot leak by
    forgetting to check. Everything below is gated on what it was given.
    """
    data: dict = {}
    if Facet.FREE_BUSY in granted:
        data["free_slots"] = ["09:00-10:00", "14:00-15:00"]
    if Facet.MEETING_SUBJECTS in granted:
        data["meetings"] = ["Q3 budget review", "1:1 with manager"]
    if Facet.WORKLOAD in granted:
        data["workload"] = "heavily loaded until Thursday"
    if Facet.TASK_STATUS in granted:
        data["open_tasks"] = 3
    if Facet.TASK_DETAIL in granted:
        data["task_titles"] = ["Reconcile INV-88213"]
    if Facet.OUT_OF_OFFICE in granted:
        data["out_of_office"] = []
    data["summary"] = f"{target.display_name} responded"
    return data


@pytest.fixture
def directory() -> AgentDirectory:
    d = AgentDirectory()
    d.register(card("alice"))
    d.register(card("bob"))
    d.register(card("supplier", organisation="supplier-external.example"))
    return d


@pytest.fixture
def contracts() -> ContractRegistry:
    return ContractRegistry()


def build_bus(directory, contracts, **kwargs) -> A2ABus:
    return A2ABus(
        directory=directory,
        contracts=contracts,
        answerer=answerer,
        principal_for=lambda uid: Principal(user_id=uid, roles=frozenset({"analyst"})),
        **kwargs,
    )


def ask(kind: RequestKind, *, frm: str = "alice", to: str = "bob", **payload) -> A2ARequest:
    return A2ARequest(from_agent=f"agent:{frm}", to_agent=f"agent:{to}", kind=kind, payload=payload)


# -- the scenario the design exists for ------------------------------------


async def test_availability_is_shared_but_subjects_are_not(directory, contracts) -> None:
    """The core promise: when Bob is free, never what his meetings are about."""
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_AVAILABILITY), requester_roles=frozenset({"colleague"})
    )

    assert response.ok
    assert response.data["free_slots"] == ["09:00-10:00", "14:00-15:00"]
    assert "meetings" not in response.data
    assert "workload" not in response.data


async def test_a_colleague_cannot_learn_what_bob_is_working_on(directory, contracts) -> None:
    """'What is she working on?' is the question that leaks by default."""
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_WORKLOAD), requester_roles=frozenset({"colleague"})
    )

    assert response.outcome is Outcome.REFUSED
    assert "workload" in response.reason


async def test_a_teammate_may_see_workload(directory, contracts) -> None:
    """A team that cannot ask each other anything routes around the assistant."""
    contracts.set(DisclosureContract(owner_id="bob", by_role={"payments-team": DEFAULT_TEAM}))
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_WORKLOAD), requester_roles=frozenset({"payments-team"})
    )

    assert response.ok
    assert response.data["workload"] == "heavily loaded until Thursday"


async def test_an_external_assistant_gets_nothing_by_default(directory, contracts) -> None:
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_AVAILABILITY, frm="supplier"), requester_roles=frozenset()
    )

    assert response.outcome is Outcome.REFUSED


async def test_partial_answers_say_what_was_withheld(directory, contracts) -> None:
    """An answer that quietly omits half the picture teaches false confidence."""
    contracts.set(DisclosureContract(owner_id="bob", default=frozenset({Facet.FREE_BUSY})))
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_AVAILABILITY), requester_roles=frozenset({"colleague"})
    )

    assert response.outcome is Outcome.PARTIAL
    assert "out of office" in response.withheld_note
    assert "withheld" in response.render()


async def test_a_named_person_can_be_denied_regardless_of_role(directory, contracts) -> None:
    contract = DisclosureContract(owner_id="bob", by_role={"payments-team": DEFAULT_TEAM})
    contract.revoke(user="alice")
    contracts.set(contract)
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_AVAILABILITY), requester_roles=frozenset({"payments-team"})
    )

    assert response.outcome is Outcome.REFUSED


async def test_a_named_grant_beats_the_default(directory, contracts) -> None:
    contract = DisclosureContract(owner_id="bob")
    contract.grant(user="alice", facets=frozenset({Facet.WORKLOAD}))
    contracts.set(contract)
    bus = build_bus(directory, contracts)

    assert (await bus.send(ask(RequestKind.ASK_WORKLOAD), requester_roles=frozenset())).ok


# -- commitments -----------------------------------------------------------


async def test_a_meeting_proposal_is_held_for_the_owner(directory, contracts) -> None:
    """A contract governs disclosure; agreeing to attend is not disclosure."""
    approvals = ApprovalStore()
    bus = build_bus(directory, contracts, approvals=approvals)

    response = await bus.send(
        ask(RequestKind.PROPOSE_MEETING, subject="incident review", slot="14:00"),
        requester_roles=frozenset({"colleague"}),
    )

    assert response.outcome is Outcome.HELD
    assert response.pending_action_id
    pending = await approvals.pending_for(Principal(user_id="bob", roles=frozenset()))
    assert len(pending) == 1


async def test_the_approval_card_names_the_person_not_the_protocol(directory, contracts) -> None:
    """Nobody approves what they cannot picture."""
    approvals = ApprovalStore()
    bus = build_bus(directory, contracts, approvals=approvals)

    await bus.send(
        ask(RequestKind.PROPOSE_MEETING, subject="incident review", slot="14:00"),
        requester_roles=frozenset({"colleague"}),
    )

    action = (await approvals.pending_for(Principal(user_id="bob", roles=frozenset())))[0]
    assert "agent:alice" in action.reason
    assert "incident review" in action.reason
    assert "14:00" in action.reason


async def test_task_delegation_is_also_a_commitment(directory, contracts) -> None:
    approvals = ApprovalStore()
    contracts.set(DisclosureContract(owner_id="bob", default=DEFAULT_TEAM))
    bus = build_bus(directory, contracts, approvals=approvals)

    response = await bus.send(
        ask(RequestKind.DELEGATE_TASK, title="Review the runbook change"),
        requester_roles=frozenset({"colleague"}),
    )

    assert response.outcome is Outcome.HELD


async def test_a_commitment_that_could_never_be_answered_is_refused_not_queued(
    directory, contracts
) -> None:
    """Do not make someone reject a request their policy already forbids."""
    contracts.set(DisclosureContract(owner_id="bob", default=frozenset()))
    approvals = ApprovalStore()
    bus = build_bus(directory, contracts, approvals=approvals)

    response = await bus.send(
        ask(RequestKind.PROPOSE_MEETING, slot="14:00"), requester_roles=frozenset()
    )

    assert response.outcome is Outcome.REFUSED
    assert await approvals.pending_for(Principal(user_id="bob", roles=frozenset())) == []


# -- capability ------------------------------------------------------------


async def test_an_assistant_that_does_not_offer_a_capability_refuses(contracts) -> None:
    directory = AgentDirectory()
    directory.register(card("alice"))
    directory.register(card("bob", caps=frozenset({Capability.AVAILABILITY})))
    bus = build_bus(directory, contracts)

    response = await bus.send(ask(RequestKind.ASK_WORKLOAD), requester_roles=frozenset())

    assert response.outcome is Outcome.REFUSED
    assert "does not answer" in response.reason


async def test_an_unknown_assistant_is_refused(directory, contracts) -> None:
    bus = build_bus(directory, contracts)

    response = await bus.send(
        ask(RequestKind.ASK_AVAILABILITY, to="ghost"), requester_roles=frozenset()
    )

    assert response.outcome is Outcome.REFUSED
    assert "no such assistant" in response.reason


# -- delegation chains -----------------------------------------------------


def test_forwarding_records_the_chain() -> None:
    original = ask(RequestKind.ASK_AVAILABILITY, frm="alice", to="bob")

    forwarded = original.forwarded_by("agent:carol")

    assert forwarded.delegation_chain == ("agent:alice",)
    assert forwarded.provenance() == "agent:alice → agent:carol"


def test_a_delegation_cycle_is_refused() -> None:
    """Two obliging assistants would otherwise pass a request forever."""
    request = ask(RequestKind.ASK_AVAILABILITY).forwarded_by("agent:carol")

    with pytest.raises(ValueError, match="cycle"):
        request.forwarded_by("agent:carol")


async def test_the_audit_records_the_whole_chain(directory, contracts) -> None:
    """'Alice asked' and 'Alice asked for someone two hops away' differ."""
    sink = InMemoryAuditSink()
    bus = build_bus(directory, contracts, audit=AuditLog(sink, record_arguments=True))
    request = ask(RequestKind.ASK_AVAILABILITY).forwarded_by("agent:carol")

    await bus.send(request, requester_roles=frozenset({"colleague"}))

    record = sink.records[0]
    assert record.arguments["chain"] == "agent:alice → agent:carol"
    assert record.principal_id == "bob"


async def test_refusals_are_audited_too(directory, contracts) -> None:
    sink = InMemoryAuditSink()
    bus = build_bus(directory, contracts, audit=AuditLog(sink))

    await bus.send(ask(RequestKind.ASK_WORKLOAD), requester_roles=frozenset())

    assert sink.records[0].outcome is AuditOutcome.DENIED


async def test_held_commitments_are_audited_as_held(directory, contracts) -> None:
    sink = InMemoryAuditSink()
    bus = build_bus(directory, contracts, audit=AuditLog(sink), approvals=ApprovalStore())

    await bus.send(
        ask(RequestKind.PROPOSE_MEETING, slot="14:00"), requester_roles=frozenset({"colleague"})
    )

    assert sink.records[0].outcome is AuditOutcome.HELD_FOR_APPROVAL


# -- contract mechanics ----------------------------------------------------


def test_roles_union_rather_than_intersect() -> None:
    """Directory roles are additive; intersecting would surprise everyone."""
    contract = DisclosureContract(
        owner_id="bob",
        by_role={
            "team": frozenset({Facet.WORKLOAD}),
            "oncall": frozenset({Facet.TASK_STATUS}),
        },
    )

    facets = contract.facets_for("alice", frozenset({"team", "oncall"}))

    assert facets == frozenset({Facet.WORKLOAD, Facet.TASK_STATUS})


def test_an_owner_without_a_contract_still_has_a_policy() -> None:
    """Someone who never opened the settings must not be wide open."""
    facets = ContractRegistry().for_owner("newcomer").facets_for("anyone", frozenset())

    assert Facet.FREE_BUSY in facets
    assert Facet.MEETING_SUBJECTS not in facets
    assert Facet.TASK_DETAIL not in facets


def test_the_default_never_includes_content() -> None:
    """Whatever else changes, the default must not reveal what things are about."""
    from uione.a2a import DEFAULT_INTERNAL, DEFAULT_TEAM

    for default in (DEFAULT_INTERNAL, DEFAULT_TEAM):
        assert Facet.MEETING_SUBJECTS not in default
        assert Facet.TASK_DETAIL not in default


def test_the_external_default_is_empty() -> None:
    from uione.a2a import DEFAULT_EXTERNAL

    assert frozenset() == DEFAULT_EXTERNAL
