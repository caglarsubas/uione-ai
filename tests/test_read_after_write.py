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
