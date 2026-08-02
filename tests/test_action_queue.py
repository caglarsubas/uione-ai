"""The unified action queue — F6.3.

The interesting assertions are the ones that keep it a *queue*. Aggregating every
system's work is easy and produces a worse inbox than the ones it replaces (G7).
"""

from __future__ import annotations

import pytest

from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
    ToolResult,
)
from uione.proactive import QueueBuilder, Urgency

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))

POLICY = ToolPolicy(
    [
        Grant(
            role="analyst",
            tools=frozenset({"tasks.*", "incidents.*", "mail.*"}),
            max_risk=RiskClass.READ,
        )
    ]
)

TICKET = {
    "key": "uione/payments#3",
    "title": "Settlement batch failing on retry",
    "updated_at": "2026-07-27T08:00:00Z",
    "url": "http://gitea.local/uione/payments/issues/3",
}
INCIDENT = {
    "key": "INC0010001",
    "title": "Card settlement delayed",
    "priority": "1",
    "updated_at": "2026-07-27T07:35:00Z",
}


def source(name: str, tool: str, rows: list[dict] | None = None, *, fail: bool = False):
    src = InMemoryToolSource(name)

    async def handler(_args: dict) -> ToolResult:
        if fail:
            return ToolResult.failure(f"{name} is unreachable")
        return ToolResult.success("rendered", {"count": len(rows or []), "items": rows or []})

    src.register(tool, handler, risk=RiskClass.READ)
    return src


async def build(*sources, approvals=None, limit: int = 20):
    gateway = McpGateway(policy=POLICY, audit=AuditLog(InMemoryAuditSink()))
    for s in sources:
        await gateway.register(s)
    return await QueueBuilder(gateway, limit=limit).build(ALICE, approvals=approvals or [])


class FakeAction:
    def __init__(self, id: str, tool: str, created_at: str = "2026-07-27T06:00:00Z") -> None:
        self.id = id
        self.tool = tool
        self.created_at = created_at


# -- what it collects ------------------------------------------------------


async def test_work_from_several_systems_lands_in_one_list() -> None:
    queue = await build(
        source("tasks", "my_open_issues", [TICKET]),
        source("incidents", "my_incidents", [INCIDENT]),
    )

    assert {i.key for i in queue.items} == {"uione/payments#3", "INC0010001"}
    assert queue.complete


async def test_a_held_action_outranks_everything() -> None:
    """The assistant is stopped one click short of done. Nothing is more urgent."""
    queue = await build(
        source("incidents", "my_incidents", [INCIDENT]),
        approvals=[FakeAction("a1", "mail.send_reply")],
    )

    assert queue.items[0].urgency is Urgency.BLOCKING_THE_ASSISTANT
    assert queue.items[0].action_id == "a1"
    assert "waiting for you" in queue.items[0].reason


async def test_a_p1_incident_outranks_ordinary_assigned_work() -> None:
    queue = await build(
        source("tasks", "my_open_issues", [TICKET]),
        source("incidents", "my_incidents", [INCIDENT]),
    )

    assert queue.items[0].key == "INC0010001"
    assert queue.items[0].urgency is Urgency.CRITICAL_INCIDENT
    assert "P1" in queue.items[0].reason


async def test_within_a_band_the_oldest_comes_first() -> None:
    """An item ignored for three days is likelier forgotten than deferred."""
    older = {**TICKET, "key": "uione/payments#1", "updated_at": "2026-07-20T08:00:00Z"}
    queue = await build(source("tasks", "my_open_issues", [older, TICKET]))

    assert [i.key for i in queue.items] == ["uione/payments#1", "uione/payments#3"]


# -- what keeps it a queue -------------------------------------------------


async def test_one_thing_seen_twice_is_one_item() -> None:
    """G7's headline: one incident, not four alerts.

    Matched on the identifier — the work graph's deterministic rule — rather than
    on similar-sounding titles, because guessing that two things are the same is
    how a queue starts hiding work.
    """
    also_a_ticket = {**TICKET, "key": "INC0010001", "title": "Card settlement delayed"}
    queue = await build(
        source("tasks", "my_open_issues", [also_a_ticket]),
        source("incidents", "my_incidents", [INCIDENT]),
    )

    assert len(queue.items) == 1
    item = queue.items[0]
    assert set(item.sources) == {"tasks", "incidents"}
    assert item.cross_system


async def test_the_more_urgent_reading_of_a_duplicate_wins() -> None:
    """An incident that is also a ticket is an incident."""
    also_a_ticket = {**TICKET, "key": "INC0010001"}
    queue = await build(
        source("tasks", "my_open_issues", [also_a_ticket]),
        source("incidents", "my_incidents", [INCIDENT]),
    )

    assert queue.items[0].urgency is Urgency.CRITICAL_INCIDENT


async def test_a_long_queue_is_capped_and_says_so() -> None:
    """Twenty is a queue; two hundred is a backlog nobody triages."""
    rows = [{**TICKET, "key": f"uione/payments#{n}"} for n in range(30)]
    queue = await build(source("tasks", "my_open_issues", rows), limit=5)

    assert len(queue.items) == 5
    assert queue.dropped == 25


async def test_the_queue_shows_only_the_mail_the_connector_selected() -> None:
    """The filtering lives in the connector, not here, because only it knows who
    the account holder is and which headers a message carried.

    This test used to assert mail never reached the queue at all, and it passed
    for the wrong reason: the policy in this module did not grant `mail.*`, so
    the source was denied rather than empty. Granting it is what surfaced the
    staleness — a stale test that passes is invisible until something changes
    underneath it.
    """
    selected = {"key": "42", "title": "Can you confirm the settlement window?"}
    queue = await build(
        source("mail", "list_unread", [selected]),
        source("tasks", "my_open_issues", [TICKET]),
    )

    assert {i.key for i in queue.items} == {"42", "uione/payments#3"}
    assert next(i for i in queue.items if i.key == "42").urgency is Urgency.AWAITING_REPLY


# -- honest degradation ----------------------------------------------------


async def test_a_dead_system_is_named_not_silently_dropped() -> None:
    """A short queue must never be mistaken for a quiet one (G8)."""
    queue = await build(
        source("tasks", "my_open_issues", [TICKET]),
        source("incidents", "my_incidents", fail=True),
    )

    assert queue.unavailable == ["incidents"]
    assert not queue.complete
    assert len(queue.items) == 1


async def test_a_system_this_deployment_does_not_have_is_not_an_outage() -> None:
    """Otherwise every queue in every deployment carries a permanent warning,
    and a banner that is always on is a banner nobody reads."""
    queue = await build(source("tasks", "my_open_issues", [TICKET]))

    assert queue.unavailable == []
    assert queue.complete


# -- permissions and cost --------------------------------------------------


async def test_the_queue_goes_through_the_gateway() -> None:
    """So it shows exactly what this person may see — the same guarantee
    retrieval makes, by the same mechanism rather than a second copy of it."""
    sink = InMemoryAuditSink()
    gateway = McpGateway(policy=POLICY, audit=AuditLog(sink))
    await gateway.register(source("tasks", "my_open_issues", [TICKET]))

    await QueueBuilder(gateway).build(ALICE)

    assert [r.tool for r in sink.records] == ["tasks.my_open_issues"]
    assert sink.records[0].principal_id == "alice"


async def test_a_principal_without_a_grant_gets_an_empty_queue_not_an_error() -> None:
    gateway = McpGateway(policy=ToolPolicy([]), audit=AuditLog(InMemoryAuditSink()))
    await gateway.register(source("tasks", "my_open_issues", [TICKET]))

    queue = await QueueBuilder(gateway).build(Principal(user_id="bob", roles=frozenset()))

    assert queue.items == []
    # Denied is not unavailable: the system is fine, this person may not use it.
    assert queue.unavailable == ["tasks"]


async def test_every_item_explains_itself() -> None:
    """A ranked list nobody can explain is one people stop trusting the moment
    its top item is wrong, and they cannot say what wrong would mean."""
    queue = await build(
        source("tasks", "my_open_issues", [TICKET]),
        source("incidents", "my_incidents", [INCIDENT]),
        approvals=[FakeAction("a1", "mail.send_reply")],
    )

    for item in queue.items:
        assert item.reason
        assert item.urgency.label


@pytest.mark.parametrize("field", ["key", "title", "urgency", "reason", "sources", "cross_system"])
async def test_the_api_shape_carries_what_a_client_needs(field: str) -> None:
    queue = await build(source("tasks", "my_open_issues", [TICKET]))

    assert field in queue.to_dict()["items"][0]


# -- mail, and why most of it stays out ------------------------------------


def message(**kwargs):
    from datetime import UTC, datetime

    from uione.connectors.mail.message import MailMessage

    defaults = {
        "uid": "1",
        "subject": "Can you confirm the settlement window?",
        "from_address": "bora@corp.example",
        "to": ["alice@corp.example"],
        "date": datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
        "unread": True,
    }
    return MailMessage(**{**defaults, **kwargs})


def awaits(msg, address: str = "alice@corp.example") -> bool:
    from uione.connectors.mail.source import awaits_reply

    return awaits_reply(msg, address)


def test_a_direct_unread_question_awaits_a_reply() -> None:
    assert awaits(message())


def test_being_copied_is_not_being_asked() -> None:
    """The condition doing most of the work. Being copied is how you are told
    something; being addressed is how you are asked something."""
    assert not awaits(message(to=["bora@corp.example"], cc=["alice@corp.example"]))


def test_a_read_message_awaits_nothing() -> None:
    assert not awaits(message(unread=False))


def test_bulk_mail_stays_out_however_it_is_addressed() -> None:
    assert not awaits(message(bulk=True))


def test_without_an_account_address_nothing_qualifies() -> None:
    """ "Addressed to you" is unanswerable without an identity, and answering it
    "yes" anyway would put the entire mailbox in the queue."""
    assert not awaits(message(), address="")


def test_the_comparison_ignores_case_and_padding() -> None:
    assert awaits(message(to=["  Alice@Corp.Example "]))


async def test_mail_reaches_the_queue_in_its_own_band() -> None:
    row = {"key": "42", "title": "Can you confirm the settlement window?"}
    queue = await build(
        source("mail", "list_unread", [row]),
        source("incidents", "my_incidents", [INCIDENT]),
    )

    mail_item = next(i for i in queue.items if i.key == "42")
    assert mail_item.urgency is Urgency.AWAITING_REPLY
    # And it sits below the P1, because a live incident outranks a question.
    assert queue.items[0].key == "INC0010001"
