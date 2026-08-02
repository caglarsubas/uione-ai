"""Read-after-write verification — F2.6.

The feature exists for one case: the connector says the write succeeded and the
system says otherwise. Everything else here is about not crying wolf, because a
verifier that reports false contradictions is one people learn to ignore.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from uione.connectors.tasks import (
    GiteaTasks,
    build_gitea_source,
    gitea_config,
    register_gitea_verification,
)
from uione.governance import ActionVerifier, Verdict
from uione.mcphub import (
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
    VerificationPlan,
)
from uione.vendormocks.gitea import build_gitea_mock, seed_gitea

ALICE = Principal(user_id="uione", roles=frozenset({"analyst"}))

WIDE_POLICY = ToolPolicy(
    [
        Grant(
            role="analyst",
            tools=frozenset({"incidents.*", "claims.*", "mail.*"}),
            max_risk=RiskClass.IRREVERSIBLE,
        )
    ]
)

POLICY = ToolPolicy(
    [
        Grant(
            role="analyst",
            tools=frozenset({"tasks.*", "widgets.*"}),
            max_risk=RiskClass.IRREVERSIBLE,
        )
    ]
)


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


# -- the mechanism ---------------------------------------------------------


def build_widgets(*, stored_state: dict) -> InMemoryToolSource:
    """A tiny two-tool system: set a value, read it back.

    ``set_value`` reports success unconditionally. What it *does* is whatever the
    test told `stored_state` to do — which is how a vendor that answers 200 and
    ignores the field is reproduced without a mock HTTP server.
    """
    source = InMemoryToolSource("widgets")

    async def set_value(args: dict) -> ToolResult:
        if stored_state.get("obedient", True):
            stored_state["value"] = args.get("value")
        return ToolResult.success(f"set to {args.get('value')}")

    async def get_value(_args: dict) -> ToolResult:
        if stored_state.get("readable", True):
            return ToolResult.success("read", {"value": stored_state.get("value")})
        return ToolResult.failure("widgets is down")

    async def slow_get_value(_args: dict) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult.success("read", {"value": stored_state.get("value")})

    source.register("set_value", set_value, risk=RiskClass.REVERSIBLE_WRITE)
    source.register("get_value", get_value, risk=RiskClass.READ)
    source.register("slow_get_value", slow_get_value, risk=RiskClass.READ)
    return source


def plan_against(read_tool: str = "widgets.get_value"):
    def builder(arguments: dict, _result: ToolResult) -> VerificationPlan:
        expected = arguments.get("value")
        return VerificationPlan(
            tool=read_tool,
            arguments={},
            expect=lambda r: (r.structured or {}).get("value") == expected,
            describes=f"the value is {expected!r}",
        )

    return builder


async def build(sink: InMemoryAuditSink, *, state: dict, read_tool: str = "widgets.get_value"):
    verifier = ActionVerifier(timeout_s=0.2)
    verifier.register("widgets.set_value", plan_against(read_tool))
    gateway = McpGateway(policy=POLICY, audit=AuditLog(sink), verifier=verifier)
    await gateway.register(build_widgets(stored_state=state))
    return gateway


async def test_a_write_that_lands_is_confirmed(sink: InMemoryAuditSink) -> None:
    gateway = await build(sink, state={"obedient": True})

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.ok
    assert call.confirmed
    assert call.verification.verdict is Verdict.CONFIRMED
    assert call.audit.outcome is AuditOutcome.ALLOWED
    assert call.audit.verified


async def test_a_write_the_system_ignored_is_contradicted(sink: InMemoryAuditSink) -> None:
    """The case the feature exists for: 200 OK, nothing changed."""
    gateway = await build(sink, state={"obedient": False, "value": "red"})

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.verification.verdict is Verdict.CONTRADICTED
    assert not call.confirmed
    assert not call.audit.verified

    # Filed as its own outcome. An auditor asking what actually happened must
    # not find these mixed in with calls that never reached the system.
    assert call.audit.outcome is AuditOutcome.UNCONFIRMED


async def test_a_contradicted_write_is_not_reported_as_a_failure(sink: InMemoryAuditSink) -> None:
    """It executed. Failing it invites a retry of something that already ran."""
    gateway = await build(sink, state={"obedient": False, "value": "red"})

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.result.ok
    assert call.result.error is None
    assert "Do not retry" in call.result.content


async def test_an_unregistered_write_is_unverifiable_not_confirmed(
    sink: InMemoryAuditSink,
) -> None:
    """The metric counts confirmations. Silence must not count as one."""
    verifier = ActionVerifier()
    gateway = McpGateway(policy=POLICY, audit=AuditLog(sink), verifier=verifier)
    await gateway.register(build_widgets(stored_state={}))

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.ok
    assert call.verification.verdict is Verdict.UNVERIFIABLE
    assert not call.confirmed
    assert not call.audit.verified
    # Nothing appended: an unverifiable write is the normal case today, and
    # narrating it in every result would train the model to hedge on everything.
    assert "[verification]" not in call.result.content


async def test_a_read_back_that_fails_is_unavailable_not_contradicted(
    sink: InMemoryAuditSink,
) -> None:
    """The system going down says nothing about whether the write landed."""
    gateway = await build(sink, state={"obedient": True, "readable": False})

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.verification.verdict is Verdict.UNAVAILABLE
    assert call.audit.outcome is AuditOutcome.ALLOWED
    assert "Could not confirm" in call.result.content


async def test_a_slow_read_back_times_out_rather_than_holding_the_reply(
    sink: InMemoryAuditSink,
) -> None:
    gateway = await build(sink, state={"obedient": True}, read_tool="widgets.slow_get_value")

    call = await asyncio.wait_for(
        gateway.call(ALICE, "widgets.set_value", {"value": "green"}), timeout=2
    )

    assert call.verification.verdict is Verdict.UNAVAILABLE
    assert call.result.ok


async def test_the_read_back_is_itself_audited(sink: InMemoryAuditSink) -> None:
    """It goes through the gateway, so it appears in the record of what was read."""
    gateway = await build(sink, state={"obedient": True})

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    tools = [r.tool for r in sink.records]
    assert "widgets.get_value" in tools

    # The read-back lands in the log *before* the write it verifies, which reads
    # backwards. That is the cost of putting the verdict on the write's own
    # record rather than emitting a second event: the record cannot be written
    # until the verdict exists. Asserted rather than left to be discovered by
    # whoever first reads a SIEM feed and wonders why the assistant read an
    # issue it had not yet touched.
    assert tools.index("widgets.get_value") < tools.index("widgets.set_value")


async def test_reads_are_not_verified(sink: InMemoryAuditSink) -> None:
    """No recursion, and no read-back traffic for calls that changed nothing."""
    gateway = await build(sink, state={"obedient": True, "value": "green"})

    call = await gateway.call(ALICE, "widgets.get_value", {})

    assert call.verification is None
    assert len(sink.records) == 1


async def test_a_broken_predicate_does_not_lose_the_write(sink: InMemoryAuditSink) -> None:
    verifier = ActionVerifier()
    verifier.register(
        "widgets.set_value",
        lambda _a, _r: VerificationPlan(
            tool="widgets.get_value",
            arguments={},
            expect=lambda _r: 1 / 0,  # noqa: B018
            describes="a predicate that raises",
        ),
    )
    gateway = McpGateway(policy=POLICY, audit=AuditLog(sink), verifier=verifier)
    await gateway.register(build_widgets(stored_state={"obedient": True}))

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.result.ok
    assert call.verification.verdict is Verdict.UNAVAILABLE


# -- against the Gitea connector -------------------------------------------


@pytest.fixture
def gitea_gateway_factory(sink: InMemoryAuditSink):
    """A gateway over the Gitea mock, with the real registered plan."""

    async def make(*, obey_state_changes: bool = True) -> McpGateway:
        app = build_gitea_mock(seed_gitea())
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gitea.mock/api/v1"
        )
        tasks = GiteaTasks(gitea_config("http://gitea.mock", "test-token"), client=client)

        if not obey_state_changes:
            # A server that accepts the PATCH, answers with the issue it would
            # have produced, and leaves the stored issue alone. Vendors do this
            # when a workflow rule reverts the transition.
            async def accept_but_ignore(owner: str, repo: str, number: int, *, state: str) -> dict:
                issue = await tasks.issue(owner, repo, number)
                return {**issue, "state": state}

            tasks.set_state = accept_but_ignore  # type: ignore[method-assign]

        verifier = ActionVerifier()
        register_gitea_verification(verifier)
        gateway = McpGateway(policy=POLICY, audit=AuditLog(sink), verifier=verifier)
        await gateway.register(build_gitea_source(tasks))
        return gateway

    return make


async def test_closing_an_issue_is_confirmed_against_the_server(gitea_gateway_factory) -> None:
    gateway = await gitea_gateway_factory()

    call = await gateway.call(
        ALICE, "tasks.update_issue", {"issue": "uione/payments-platform#2", "state": "closed"}
    )

    assert call.ok
    assert call.verification.verdict is Verdict.CONFIRMED
    assert call.audit.verified


async def test_a_server_that_reverts_the_transition_is_caught(gitea_gateway_factory) -> None:
    """The connector reports the issue closed. The server still has it open.

    Without the read-back this is indistinguishable from success, and the user
    is told their ticket is closed.
    """
    gateway = await gitea_gateway_factory(obey_state_changes=False)

    call = await gateway.call(
        ALICE, "tasks.update_issue", {"issue": "uione/payments-platform#2", "state": "closed"}
    )

    assert call.verification.verdict is Verdict.CONTRADICTED
    assert call.audit.outcome is AuditOutcome.UNCONFIRMED
    assert "uione/payments-platform#2 is closed" in call.verification.detail


async def test_an_unparseable_reference_is_not_read_back(gitea_gateway_factory) -> None:
    """The write refused it, so there is no record to check."""
    gateway = await gitea_gateway_factory()

    call = await gateway.call(ALICE, "tasks.update_issue", {"issue": "nonsense", "state": "closed"})

    assert not call.ok
    assert call.verification is None


# -- the rest of the write-capable estate ----------------------------------


async def gateway_over(source, register) -> McpGateway:
    """A gateway with one connector and its own registered read-back."""
    verifier = ActionVerifier()
    register(verifier)
    gateway = McpGateway(policy=WIDE_POLICY, audit=AuditLog(InMemoryAuditSink()), verifier=verifier)
    await gateway.register(source)
    return gateway


async def test_a_servicenow_state_change_is_confirmed() -> None:
    from uione.connectors.incidents import (
        ServiceNowIncidents,
        build_servicenow_source,
        register_servicenow_verification,
        servicenow_config,
    )
    from uione.vendormocks.servicenow import build_servicenow_mock, seed_servicenow

    app = build_servicenow_mock(seed_servicenow())
    incidents = ServiceNowIncidents(
        servicenow_config("http://snow.mock", "admin", "pw"),
        user="uione",
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://snow.mock"
        ),
    )
    gateway = await gateway_over(
        build_servicenow_source(incidents), register_servicenow_verification
    )

    call = await gateway.call(
        ALICE, "incidents.update_incident", {"incident": "INC0010001", "state": "in_progress"}
    )

    assert call.ok
    assert call.verification.verdict is Verdict.CONFIRMED


async def test_a_work_note_alone_has_nothing_to_read_back() -> None:
    """`get_incident` does not return the journal, so this is honestly
    unverifiable rather than quietly confirmed."""
    from uione.connectors.incidents import (
        ServiceNowIncidents,
        build_servicenow_source,
        register_servicenow_verification,
        servicenow_config,
    )
    from uione.vendormocks.servicenow import build_servicenow_mock, seed_servicenow

    app = build_servicenow_mock(seed_servicenow())
    incidents = ServiceNowIncidents(
        servicenow_config("http://snow.mock", "admin", "pw"),
        user="uione",
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://snow.mock"
        ),
    )
    gateway = await gateway_over(
        build_servicenow_source(incidents), register_servicenow_verification
    )

    call = await gateway.call(
        ALICE, "incidents.update_incident", {"incident": "INC0010001", "work_note": "looking"}
    )

    assert call.ok
    assert call.verification.verdict is Verdict.UNVERIFIABLE


async def test_a_claim_status_change_is_confirmed() -> None:
    from uione.connectors.claims import (
        ClaimsBackend,
        build_claims_source,
        claims_config,
        register_claims_verification,
    )
    from uione.vendormocks.claims import build_claims_mock, seed_claims

    app = build_claims_mock(seed_claims())
    claims = ClaimsBackend(
        claims_config("http://claims.mock", ""),
        user="uione",
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://claims.mock"
        ),
    )
    gateway = await gateway_over(build_claims_source(claims), register_claims_verification)

    call = await gateway.call(
        ALICE, "claims.set_status", {"claim": "CLM-004401", "status": "reopened"}
    )

    assert call.ok
    assert call.verification.verdict is Verdict.CONFIRMED


# -- absence, which is the interesting shape -------------------------------


def mail_backend(count: int = 3):
    from datetime import UTC, datetime

    from uione.connectors.mail import InMemoryMailBackend
    from uione.connectors.mail.message import MailMessage

    return InMemoryMailBackend(
        messages=[
            MailMessage(
                uid=str(i),
                subject=f"Message {i}",
                from_address="cfo@corp.example",
                body="body",
                date=datetime(2026, 7, 27, 8, i % 60, tzinfo=UTC),
                unread=True,
            )
            for i in range(1, count + 1)
        ]
    )


async def test_marking_read_is_confirmed_by_the_message_leaving_the_unread_list() -> None:
    from uione.connectors.mail import build_mail_source, register_mail_verification

    gateway = await gateway_over(build_mail_source(mail_backend()), register_mail_verification)

    call = await gateway.call(ALICE, "mail.mark_read", {"uid": "2"})

    assert call.ok
    assert call.verification.verdict is Verdict.CONFIRMED
    assert "no longer unread" in call.verification.detail


async def test_a_message_still_unread_is_contradicted() -> None:
    """A backend that accepts the flag and does not apply it."""
    from uione.connectors.mail import build_mail_source, register_mail_verification

    backend = mail_backend()

    async def accept_and_ignore(uid: str) -> None:
        return None

    backend.mark_read = accept_and_ignore  # type: ignore[method-assign]
    gateway = await gateway_over(build_mail_source(backend), register_mail_verification)

    call = await gateway.call(ALICE, "mail.mark_read", {"uid": "2"})

    assert call.verification.verdict is Verdict.CONTRADICTED
    assert call.audit.outcome is AuditOutcome.UNCONFIRMED


async def test_absence_against_a_truncated_list_is_unavailable_not_confirmed() -> None:
    """The check that keeps this honest.

    With the mailbox at or above the read ceiling, a uid missing from the list
    could be genuinely read or could be sitting just past the cut. Confirming
    there would manufacture a verification out of a list nobody finished reading
    — which is the exact failure this whole feature exists to prevent.
    """
    from uione.connectors.mail import build_mail_source, register_mail_verification
    from uione.connectors.mail.source import MAX_LIMIT

    gateway = await gateway_over(
        build_mail_source(mail_backend(MAX_LIMIT + 5)), register_mail_verification
    )

    call = await gateway.call(ALICE, "mail.mark_read", {"uid": "2"})

    assert call.ok
    assert call.verification.verdict is Verdict.UNAVAILABLE
    assert not call.audit.verified
