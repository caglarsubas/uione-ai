"""The MCP gateway — the single chokepoint between agents and enterprise systems.

Architectural commitment: no agent code calls a connector directly. Everything
arrives here, so authentication, tool allow-listing, rate limiting, circuit
breaking, and the audit tap exist in exactly one place instead of being
re-implemented (and forgotten) per connector.

The class is written so that every exit path audits. If you add a branch that
returns without an audit record, the tests in ``test_mcphub_gateway.py`` fail.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from uione.mcphub.audit import AuditLog, AuditOutcome, AuditRecord
from uione.mcphub.policy import RateLimiter, ToolPolicy
from uione.mcphub.source import ToolSource
from uione.mcphub.types import ActionContext, Principal, RiskClass, ToolResult, ToolSpec
from uione.modelplane.types import ToolDefinition

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GovernanceVerdict:
    """Whether an action may execute now, and why."""

    allowed: bool
    reason: str = ""
    pending_action_id: str | None = None


class ActionGovernor(Protocol):
    """Decides whether a permitted action may run *unattended*.

    Declared here rather than imported from :mod:`uione.governance` so the
    dependency points one way: the gateway owns the contract, governance
    implements it. That keeps the chokepoint free of policy detail while making
    it impossible to reach a connector without passing this check.
    """

    async def authorize(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict,
        context: ActionContext,
    ) -> GovernanceVerdict: ...

    async def note_execution(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict,
        result: ToolResult,
    ) -> None: ...


@dataclass
class CircuitBreaker:
    """Stops hammering a connector that is already failing.

    Without this, one dead connector turns every morning brief into a wall of
    timeouts: 500 users × retries against a server that is not coming back.
    """

    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def is_open(self, server: str) -> bool:
        opened = self._opened_at.get(server)
        if opened is None:
            return False
        if self.clock() - opened >= self.cooldown_s:
            # Half-open: let one call through to test the water.
            del self._opened_at[server]
            self._failures[server] = self.failure_threshold - 1
            return False
        return True

    def record_success(self, server: str) -> None:
        self._failures.pop(server, None)
        self._opened_at.pop(server, None)

    def record_failure(self, server: str) -> None:
        count = self._failures.get(server, 0) + 1
        self._failures[server] = count
        if count >= self.failure_threshold:
            self._opened_at[server] = self.clock()
            log.warning("mcphub.circuit_opened", server=server, failures=count)


class ToolNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class GatewayCall:
    """A completed call, result plus the audit record that describes it."""

    result: ToolResult
    audit: AuditRecord
    pending_action_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def held(self) -> bool:
        """True when the action was withheld pending human approval."""
        return self.audit.outcome is AuditOutcome.HELD_FOR_APPROVAL


class McpGateway:
    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        audit: AuditLog | None = None,
        rate_limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        governor: ActionGovernor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources: dict[str, ToolSource] = {}
        self._catalog: dict[str, ToolSpec] = {}
        self._policy = policy or ToolPolicy()
        self._audit = audit or AuditLog()
        self._rate_limiter = rate_limiter or RateLimiter()
        self._breaker = breaker or CircuitBreaker()
        self._governor = governor
        self._clock = clock

    # -- registration ------------------------------------------------------

    async def register(self, source: ToolSource) -> list[ToolSpec]:
        """Add a source and index its tools."""
        self._sources[source.name] = source
        specs = await source.list_tools()
        for spec in specs:
            self._catalog[spec.qualified_name] = spec
        log.info("mcphub.registered", server=source.name, tools=len(specs))
        return specs

    @property
    def catalog(self) -> list[ToolSpec]:
        return list(self._catalog.values())

    def has_tool(self, qualified_name: str) -> bool:
        return qualified_name in self._catalog

    def spec(self, qualified_name: str) -> ToolSpec:
        try:
            return self._catalog[qualified_name]
        except KeyError:
            raise ToolNotFoundError(qualified_name) from None

    # -- exposure ----------------------------------------------------------

    def tools_for(self, principal: Principal) -> list[ToolSpec]:
        return self._policy.visible_tools(principal, self._catalog.values())

    def tool_definitions_for(self, principal: Principal) -> list[ToolDefinition]:
        """What this principal's model prompt is allowed to see.

        Deliberately not the whole catalog: showing a model tools its user cannot
        use produces confident proposals that fail at execution.
        """
        return [s.to_tool_definition() for s in self.tools_for(principal)]

    # -- invocation --------------------------------------------------------

    async def call(
        self,
        principal: Principal,
        qualified_name: str,
        arguments: dict | None = None,
        *,
        correlation_id: str | None = None,
        context: ActionContext | None = None,
    ) -> GatewayCall:
        """Invoke a tool on behalf of a principal.

        Never raises for an expected refusal — an unknown tool, a denial, a rate
        limit, or an action held for approval come back as a failed
        :class:`ToolResult` so the agent loop can show the model why and let it
        choose differently.
        """
        arguments = arguments or {}
        context = context or ActionContext(correlation_id=correlation_id)

        spec = self._catalog.get(qualified_name)
        if spec is None:
            record = await self._audit.record(
                principal=principal,
                server=qualified_name.split(".", 1)[0] if "." in qualified_name else "unknown",
                tool=qualified_name,
                risk=RiskClass.READ,
                outcome=AuditOutcome.UNKNOWN_TOOL,
                arguments=arguments,
                detail="tool not in catalog",
                correlation_id=correlation_id,
            )
            return GatewayCall(ToolResult.failure(f"unknown tool {qualified_name!r}"), record)

        if not self._policy.allows(principal, spec):
            record = await self._audit.record(
                principal=principal,
                server=spec.server,
                tool=spec.qualified_name,
                risk=spec.risk,
                outcome=AuditOutcome.DENIED,
                arguments=arguments,
                detail=f"principal lacks a grant covering {spec.risk} on {spec.server}",
                correlation_id=correlation_id,
            )
            return GatewayCall(
                ToolResult.failure(f"not permitted to call {qualified_name!r}"), record
            )

        if not self._rate_limiter.check(principal, spec.qualified_name):
            record = await self._audit.record(
                principal=principal,
                server=spec.server,
                tool=spec.qualified_name,
                risk=spec.risk,
                outcome=AuditOutcome.RATE_LIMITED,
                arguments=arguments,
                detail="per-principal rate limit exceeded",
                correlation_id=correlation_id,
            )
            return GatewayCall(
                ToolResult.failure(f"rate limit exceeded for {qualified_name!r}"), record
            )

        if self._breaker.is_open(spec.server):
            record = await self._audit.record(
                principal=principal,
                server=spec.server,
                tool=spec.qualified_name,
                risk=spec.risk,
                outcome=AuditOutcome.CIRCUIT_OPEN,
                arguments=arguments,
                detail=f"circuit open for {spec.server}",
                correlation_id=correlation_id,
            )
            return GatewayCall(
                ToolResult.failure(f"{spec.server} is unavailable (circuit open)"), record
            )

        # Governance is consulted last, immediately before execution, so nothing
        # can reach a connector without passing it.
        if self._governor is not None:
            verdict = await self._governor.authorize(principal, spec, arguments, context)
            if not verdict.allowed:
                record = await self._audit.record(
                    principal=principal,
                    server=spec.server,
                    tool=spec.qualified_name,
                    risk=spec.risk,
                    outcome=AuditOutcome.HELD_FOR_APPROVAL,
                    arguments=arguments,
                    detail=verdict.reason,
                    correlation_id=context.correlation_id or correlation_id,
                )
                return GatewayCall(
                    ToolResult.failure(
                        f"This action needs your approval before it runs: {verdict.reason}"
                    ),
                    record,
                    pending_action_id=verdict.pending_action_id,
                )

        source = self._sources[spec.server]

        started = self._clock()
        try:
            # The caller is an argument, not state the source carries between
            # calls: two users are in flight at once and the second must not be
            # able to overwrite the first's identity mid-call. See `source.py`.
            result = await source.call(spec.tool, arguments, principal=principal)
        except Exception as exc:  # noqa: BLE001 — a connector must not crash the agent
            self._breaker.record_failure(spec.server)
            record = await self._audit.record(
                principal=principal,
                server=spec.server,
                tool=spec.qualified_name,
                risk=spec.risk,
                outcome=AuditOutcome.FAILED,
                arguments=arguments,
                duration_ms=(self._clock() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
                correlation_id=correlation_id,
            )
            return GatewayCall(ToolResult.failure(f"{type(exc).__name__}: {exc}"), record)

        duration_ms = (self._clock() - started) * 1000
        if result.ok:
            self._breaker.record_success(spec.server)
        else:
            self._breaker.record_failure(spec.server)

        if self._governor is not None:
            await self._governor.note_execution(principal, spec, arguments, result)

        record = await self._audit.record(
            principal=principal,
            server=spec.server,
            tool=spec.qualified_name,
            risk=spec.risk,
            outcome=AuditOutcome.ALLOWED if result.ok else AuditOutcome.FAILED,
            arguments=arguments,
            duration_ms=duration_ms,
            detail=result.error,
            correlation_id=context.correlation_id or correlation_id,
        )
        return GatewayCall(result, record)

    # -- health ------------------------------------------------------------

    def server_health(self) -> dict[str, str]:
        """Per-server status, for the honest-degradation surface (gap G8)."""
        return {
            name: ("unavailable" if self._breaker.is_open(name) else "ok") for name in self._sources
        }

    def degraded_servers(self) -> Iterable[str]:
        return (name for name, status in self.server_health().items() if status != "ok")
