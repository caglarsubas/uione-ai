from __future__ import annotations

import asyncio

import pytest

from uione.mcphub import (
    AuditLog,
    AuditOutcome,
    CircuitBreaker,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RateLimiter,
    RiskClass,
    ToolPolicy,
    ToolResult,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")
BOB = Principal(user_id="bob", roles=frozenset({"guest"}))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_source(*, fail: bool = False, raises: bool = False) -> InMemoryToolSource:
    source = InMemoryToolSource("mail")

    async def search(args: dict) -> ToolResult:
        if raises:
            raise ConnectionError("connector exploded")
        if fail:
            return ToolResult.failure("mailbox unavailable")
        return ToolResult.success(f"found messages for {args.get('query')}")

    async def send(args: dict) -> ToolResult:
        return ToolResult.success(f"sent to {args.get('to')}")

    source.register(
        "search",
        search,
        description="Search mail",
        risk=RiskClass.READ,
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    source.register("send", send, description="Send mail", risk=RiskClass.EXTERNAL_FACING)
    return source


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


async def build_gateway(sink: InMemoryAuditSink, **kwargs) -> McpGateway:
    policy = kwargs.pop("policy", None) or ToolPolicy(
        [Grant(role="analyst", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ)]
    )
    source = kwargs.pop("source", None) or build_source()
    gateway = McpGateway(policy=policy, audit=AuditLog(sink), **kwargs)
    await gateway.register(source)
    return gateway


# -- happy path ------------------------------------------------------------


async def test_allowed_call_executes_and_is_audited(sink: InMemoryAuditSink) -> None:
    gateway = await build_gateway(sink)

    call = await gateway.call(ALICE, "mail.search", {"query": "budget"})

    assert call.ok
    assert "budget" in call.result.content
    assert call.audit.outcome is AuditOutcome.ALLOWED
    assert len(sink.records) == 1
    assert sink.records[0].principal_id == "alice"


async def test_tools_are_namespaced_by_server(sink: InMemoryAuditSink) -> None:
    """Two connectors will both offer 'search'; the model must not conflate them."""
    gateway = await build_gateway(sink)
    assert {s.qualified_name for s in gateway.catalog} == {"mail.search", "mail.send"}


# -- the audit invariant ---------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "principal", "expected"),
    [
        ("mail.nonexistent", ALICE, AuditOutcome.UNKNOWN_TOOL),
        ("mail.send", ALICE, AuditOutcome.DENIED),  # grant caps at READ
        ("mail.search", BOB, AuditOutcome.DENIED),  # no grant at all
    ],
)
async def test_every_refusal_path_is_audited(
    sink: InMemoryAuditSink, tool: str, principal: Principal, expected: AuditOutcome
) -> None:
    gateway = await build_gateway(sink)

    call = await gateway.call(principal, tool)

    assert not call.ok
    assert len(sink.records) == 1
    assert sink.records[0].outcome is expected


async def test_connector_failure_is_audited(sink: InMemoryAuditSink) -> None:
    gateway = await build_gateway(sink, source=build_source(fail=True))

    call = await gateway.call(ALICE, "mail.search", {"query": "x"})

    assert not call.ok
    assert sink.records[0].outcome is AuditOutcome.FAILED


async def test_connector_exception_does_not_escape(sink: InMemoryAuditSink) -> None:
    """A broken connector must degrade the assistant, not crash the request."""
    gateway = await build_gateway(sink, source=build_source(raises=True))

    call = await gateway.call(ALICE, "mail.search", {"query": "x"})

    assert not call.ok
    assert "ConnectionError" in (call.result.error or "")
    assert sink.records[0].outcome is AuditOutcome.FAILED


async def test_arguments_are_hashed_not_stored_by_default(sink: InMemoryAuditSink) -> None:
    """An audit log that is itself a PII spill helps nobody."""
    gateway = await build_gateway(sink)

    await gateway.call(ALICE, "mail.search", {"query": "patient records"})

    record = sink.records[0]
    assert record.arguments is None
    assert len(record.arguments_hash) == 32


async def test_argument_capture_is_opt_in(sink: InMemoryAuditSink) -> None:
    gateway = McpGateway(
        policy=ToolPolicy([Grant(role="analyst", tools=frozenset({"mail.*"}))]),
        audit=AuditLog(sink, record_arguments=True),
    )
    await gateway.register(build_source())

    await gateway.call(ALICE, "mail.search", {"query": "secret"})

    assert sink.records[0].arguments == {"query": "secret"}


async def test_identical_calls_hash_identically(sink: InMemoryAuditSink) -> None:
    gateway = await build_gateway(sink)

    await gateway.call(ALICE, "mail.search", {"query": "a", "limit": 5})
    await gateway.call(ALICE, "mail.search", {"limit": 5, "query": "a"})

    assert sink.records[0].arguments_hash == sink.records[1].arguments_hash


# -- policy ----------------------------------------------------------------


async def test_deny_by_default(sink: InMemoryAuditSink) -> None:
    gateway = await build_gateway(sink, policy=ToolPolicy())

    call = await gateway.call(ALICE, "mail.search")

    assert not call.ok
    assert sink.records[0].outcome is AuditOutcome.DENIED


async def test_wildcard_grant_is_capped_by_risk(sink: InMemoryAuditSink) -> None:
    """A broad read grant must not silently confer the ability to send mail."""
    gateway = await build_gateway(sink)

    assert (await gateway.call(ALICE, "mail.search")).ok
    assert not (await gateway.call(ALICE, "mail.send", {"to": "x@y.z"})).ok


async def test_explicit_grant_overrides_the_risk_cap(sink: InMemoryAuditSink) -> None:
    policy = ToolPolicy([Grant(role="analyst", tools=frozenset({"mail.send"}))])
    gateway = await build_gateway(sink, policy=policy)

    assert (await gateway.call(ALICE, "mail.send", {"to": "x@y.z"})).ok


async def test_model_only_sees_permitted_tools(sink: InMemoryAuditSink) -> None:
    """Proposing actions the user cannot take reads as confusion and wastes a turn."""
    gateway = await build_gateway(sink)

    names = [d.name for d in gateway.tool_definitions_for(ALICE)]

    assert names == ["mail.search"]
    assert gateway.tool_definitions_for(BOB) == []


# -- rate limiting ---------------------------------------------------------


async def test_rate_limit_bounds_a_runaway_loop(sink: InMemoryAuditSink) -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=3, refill_per_second=0.0, clock=clock)
    gateway = await build_gateway(sink, rate_limiter=limiter)

    outcomes = [(await gateway.call(ALICE, "mail.search")).ok for _ in range(5)]

    assert outcomes == [True, True, True, False, False]
    assert len(sink.with_outcome(AuditOutcome.RATE_LIMITED)) == 2


async def test_rate_limit_refills_over_time(sink: InMemoryAuditSink) -> None:
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    gateway = await build_gateway(sink, rate_limiter=limiter)

    for _ in range(2):
        await gateway.call(ALICE, "mail.search")
    assert not (await gateway.call(ALICE, "mail.search")).ok

    clock.advance(5)

    assert (await gateway.call(ALICE, "mail.search")).ok


async def test_rate_limits_are_per_principal(sink: InMemoryAuditSink) -> None:
    """One noisy user must not lock out everyone else."""
    clock = FakeClock()
    policy = ToolPolicy(
        [
            Grant(role="analyst", tools=frozenset({"mail.*"})),
            Grant(role="guest", tools=frozenset({"mail.*"})),
        ]
    )
    limiter = RateLimiter(capacity=1, refill_per_second=0.0, clock=clock)
    gateway = await build_gateway(sink, policy=policy, rate_limiter=limiter)

    assert (await gateway.call(ALICE, "mail.search")).ok
    assert not (await gateway.call(ALICE, "mail.search")).ok
    assert (await gateway.call(BOB, "mail.search")).ok


# -- circuit breaker -------------------------------------------------------


async def test_circuit_opens_after_repeated_failures(sink: InMemoryAuditSink) -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=30, clock=clock)
    gateway = await build_gateway(sink, source=build_source(fail=True), breaker=breaker)

    await gateway.call(ALICE, "mail.search")
    await gateway.call(ALICE, "mail.search")
    call = await gateway.call(ALICE, "mail.search")

    assert sink.records[-1].outcome is AuditOutcome.CIRCUIT_OPEN
    assert "circuit open" in (call.result.error or "")


async def test_circuit_half_opens_after_cooldown(sink: InMemoryAuditSink) -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=30, clock=clock)
    source = build_source(fail=True)
    gateway = await build_gateway(sink, source=source, breaker=breaker)

    await gateway.call(ALICE, "mail.search")
    assert sink.records[-1].outcome is AuditOutcome.FAILED
    assert (await gateway.call(ALICE, "mail.search")).audit.outcome is AuditOutcome.CIRCUIT_OPEN

    clock.advance(31)

    # Half-open lets one probe through rather than staying dark forever.
    assert (await gateway.call(ALICE, "mail.search")).audit.outcome is AuditOutcome.FAILED


async def test_success_resets_the_breaker(sink: InMemoryAuditSink) -> None:
    breaker = CircuitBreaker(failure_threshold=2)
    source = InMemoryToolSource("mail")
    state = {"fail": True}

    async def flaky(_args: dict) -> ToolResult:
        return ToolResult.failure("down") if state["fail"] else ToolResult.success("up")

    source.register("search", flaky, risk=RiskClass.READ)
    gateway = await build_gateway(sink, source=source, breaker=breaker)

    await gateway.call(ALICE, "mail.search")
    state["fail"] = False
    await gateway.call(ALICE, "mail.search")
    state["fail"] = True
    await gateway.call(ALICE, "mail.search")

    # One failure before and after a success must not add up to an open circuit.
    assert sink.records[-1].outcome is AuditOutcome.FAILED


async def test_an_uncalled_connector_is_unknown_not_ok(sink: InMemoryAuditSink) -> None:
    """This assertion used to read `== {"mail": "ok"}`, and that was the bug.

    Health was derived from the circuit breaker, which is a question about our
    retry policy rather than about the connector. A server nobody had called
    reported healthy; so did one that had failed four times, because the breaker
    opens on the fifth. A live deployment showed three connectors failing in the
    UI while /system/health returned all-green with `degraded: []`.

    "ok" is a claim that the last call succeeded. Absent a call, there is no
    claim to make.
    """
    gateway = await build_gateway(sink, source=build_source(fail=True))

    assert gateway.server_health() == {"mail": "unknown"}
    # And unknown is not degraded — a connector nobody uses is unexercised, not
    # broken, and listing it would put a permanent warning on every deployment.
    assert list(gateway.degraded_servers()) == []


async def test_the_first_failure_is_visible_not_the_fifth(sink: InMemoryAuditSink) -> None:
    """The beginning of an outage is the only part a health endpoint is for."""
    gateway = await build_gateway(sink, source=build_source(fail=True))

    await gateway.call(ALICE, "mail.search")

    assert gateway.server_health() == {"mail": "failing"}
    assert list(gateway.degraded_servers()) == ["mail"]
    assert gateway.server_details()["mail"]["consecutive_failures"] == 1


async def test_the_reason_travels_with_the_status(sink: InMemoryAuditSink) -> None:
    """ "tasks is failing" sends somebody to read logs; "tasks is failing: gitea
    unreachable" sends them to start gitea."""
    gateway = await build_gateway(sink, source=build_source(fail=True))

    await gateway.call(ALICE, "mail.search")

    assert "unavailable" in (gateway.server_details()["mail"]["last_error"] or "")


async def test_recovery_clears_the_failure(sink: InMemoryAuditSink) -> None:
    source = InMemoryToolSource("mail")
    state = {"fail": True}

    async def flaky(_args: dict) -> ToolResult:
        return ToolResult.failure("down") if state["fail"] else ToolResult.success("up")

    source.register("search", flaky, risk=RiskClass.READ)
    gateway = await build_gateway(sink, source=source)

    await gateway.call(ALICE, "mail.search")
    assert gateway.server_health() == {"mail": "failing"}

    state["fail"] = False
    await gateway.call(ALICE, "mail.search")

    assert gateway.server_health() == {"mail": "ok"}
    assert gateway.server_details()["mail"]["last_error"] is None


async def test_a_breaker_that_gave_up_says_unavailable(sink: InMemoryAuditSink) -> None:
    """Distinct from `failing`: we have stopped trying, so the next call will not
    even reach the connector."""
    breaker = CircuitBreaker(failure_threshold=1)
    gateway = await build_gateway(sink, source=build_source(fail=True), breaker=breaker)

    await gateway.call(ALICE, "mail.search")

    assert gateway.server_health() == {"mail": "unavailable"}
    assert list(gateway.degraded_servers()) == ["mail"]


# -- the caller travels with the call --------------------------------------


async def test_concurrent_callers_do_not_see_each_other(sink: InMemoryAuditSink) -> None:
    """Two users in flight at once, and the handler reads its caller *after* an
    await.

    This is the regression test for a real hazard in the previous design, where
    the gateway wrote the principal into a dict the source closed over and then
    invoked the handler. That was safe only while every handler read the slot
    before its first suspension point — an invariant nothing enforced. A handler
    shaped like this one saw whichever principal had most recently entered the
    gateway, so Alice's retrieval ran with Bob's permissions.
    """
    source = InMemoryToolSource("mail")

    async def whoami(_args: dict, principal: Principal) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult.success(principal.user_id)

    source.register("whoami", whoami, risk=RiskClass.READ, identified=True)

    policy = ToolPolicy(
        [
            Grant(role="analyst", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ),
            Grant(role="guest", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ),
        ]
    )
    gateway = await build_gateway(sink, source=source, policy=policy)

    alice, bob = await asyncio.gather(
        gateway.call(ALICE, "mail.whoami"),
        gateway.call(BOB, "mail.whoami"),
    )

    assert (alice.result.content, bob.result.content) == ("alice", "bob")


async def test_an_identified_tool_refuses_an_anonymous_call() -> None:
    """Reached without a caller, an identity-filtered tool fails rather than
    running as nobody — which for a retrieval tool would mean running as everyone.
    """
    source = InMemoryToolSource("knowledge")

    async def search(_args: dict, principal: Principal) -> ToolResult:
        return ToolResult.success(principal.user_id)

    source.register("search", search, risk=RiskClass.READ, identified=True)

    result = await source.call("search", {})

    assert not result.ok
    assert "identified caller" in (result.error or "")
