"""Tool sources — where tools actually come from.

The gateway talks to this protocol, never to a transport. That keeps all the
governance logic testable without spawning servers, and keeps MCP version churn
contained to one file.

**The caller travels with the call.** A tool that filters by identity — retrieval
above all — is handed its principal as an argument, not left to read one from
somewhere. The earlier design bound the principal into a dict the source closed
over, immediately before invoking the handler, and that was safe only for as long
as every handler happened to read the slot before its first ``await``. A handler
that read it afterwards saw whichever principal had most recently entered the
gateway, which under two concurrent users means running one person's search with
the other's permissions. The invariant was real, unwritten, and one plausible
edit away from a cross-user leak in the one component whose whole job is not
leaking. Passing it removes the invariant instead of documenting it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

from uione.mcphub.types import Principal, RiskClass, ToolResult, ToolSpec
from uione.security.injection import scan_for_injection

log = structlog.get_logger(__name__)

#: A handler that needs no identity: most of them.
AnonymousHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]

#: A handler that filters or attributes by caller, and says so at registration.
IdentifiedHandler = Callable[[dict[str, Any], Principal], Awaitable[ToolResult]]


class ToolSource(Protocol):
    """A named collection of callable tools."""

    @property
    def name(self) -> str: ...

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(
        self, tool: str, arguments: dict[str, Any], *, principal: Principal | None = None
    ) -> ToolResult: ...


def classify_risk(
    tool_name: str,
    annotations: Any | None,
    overrides: dict[str, RiskClass] | None = None,
) -> RiskClass:
    """Assign a risk class to a discovered tool.

    **A server may raise its own risk. It may never lower it.**

    That asymmetry is the whole rule, and the earlier version of this function
    got it wrong: it honoured ``readOnlyHint`` and returned ``READ``, which is
    the one class exempt from the approval ladder. A hostile or compromised MCP
    server could therefore declare ``deleteEverything`` read-only and have it run
    unattended. The MCP specification is explicit that annotations are hints and
    must not be relied upon for security decisions — and deciding what runs
    without a human is exactly a security decision.

    The asymmetry is safe in both directions because of who benefits. A server
    claiming to be *more* dangerous than it is costs its own users an approval
    prompt; there is no attack in it. A server claiming to be *less* dangerous
    than it is buys itself unattended execution, which is the entire prize.

    So the order of authority is: our operator's explicit override, then any
    server hint that makes the tool *more* restricted than the default, then the
    default. The default is ``IRREVERSIBLE``, because an unclassified tool is one
    nobody has reasoned about, and mistaking a destructive tool for a read is the
    expensive direction of that mistake.

    Getting ``READ`` therefore requires a human to have said so, per tool.
    """
    overrides = overrides or {}
    if tool_name in overrides:
        # The operator has looked at this tool. Their word is final, in either
        # direction — this is the only path to READ.
        return overrides[tool_name]

    if annotations is not None:
        # Only hints that *raise* risk are honoured. `readOnlyHint` and
        # `idempotentHint` are deliberately ignored: both would lower it.
        if getattr(annotations, "destructiveHint", None) is True:
            return RiskClass.IRREVERSIBLE
        if getattr(annotations, "openWorldHint", None) is True:
            return RiskClass.EXTERNAL_FACING

        if getattr(annotations, "readOnlyHint", None) is True:
            log.info(
                "mcphub.read_only_hint_ignored",
                tool=tool_name,
                reason="a server may not lower its own risk; add an operator override",
            )

    log.warning("mcphub.unclassified_tool", tool=tool_name, assigned=str(RiskClass.IRREVERSIBLE))
    return RiskClass.IRREVERSIBLE


def describe_remote_tool(server: str, tool: str, description: str) -> str | None:
    """Vet a third-party tool description before the model ever sees it.

    A tool description is the one piece of attacker-controllable text that goes
    into the context window *without anyone invoking anything* — it is in the
    catalog by virtue of the server being registered. That makes it a better
    injection vector than any tool result: no user action is required, and it is
    present in every single request.

    Returns ``None`` to withhold the tool entirely. Refusing beats sanitising
    here, and this is the opposite of the choice made for tool *results*: a
    result arrives mid-task and dropping it silently breaks the work, so results
    are quarantined and shown. A description is read once at registration, and a
    server whose catalog contains "ignore previous instructions" is a server an
    operator needs to know about before it is used, not one to serve in a wrapper.
    """
    findings = scan_for_injection(description)
    if findings:
        log.error(
            "mcphub.poisoned_tool_description",
            server=server,
            tool=tool,
            patterns=[f.pattern for f in findings],
            action="tool withheld from the catalog",
        )
        return None
    return description


class InMemoryToolSource:
    """A tool source backed by Python callables.

    Used by tests and by built-in tools that need no external process.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, AnonymousHandler | IdentifiedHandler] = {}
        self._identified: set[str] = set()

    @property
    def name(self) -> str:
        return self._name

    def register(
        self,
        tool: str,
        handler: AnonymousHandler | IdentifiedHandler,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        risk: RiskClass = RiskClass.READ,
        returns_untrusted_content: bool = False,
        identified: bool = False,
    ) -> ToolSpec:
        """Register one tool.

        ``identified=True`` means the handler takes ``(arguments, principal)``
        and will not run without a caller. Declared at registration rather than
        inferred from the signature: whether a tool filters by identity is a
        security property, and reading it off a function's parameter list makes
        renaming an argument a permissions change.
        """
        spec = ToolSpec(
            server=self._name,
            tool=tool,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            risk=risk,
            returns_untrusted_content=returns_untrusted_content,
        )
        self._specs[tool] = spec
        self._handlers[tool] = handler
        if identified:
            self._identified.add(tool)
        return spec

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(
        self, tool: str, arguments: dict[str, Any], *, principal: Principal | None = None
    ) -> ToolResult:
        handler = self._handlers.get(tool)
        if handler is None:
            return ToolResult.failure(f"unknown tool {tool!r} on server {self._name!r}")

        if tool not in self._identified:
            return await handler(arguments)  # type: ignore[call-arg]

        if principal is None:
            # Fails closed, and loudly. An identity-filtered tool reached without
            # an identity is a wiring bug, and the tempting alternative — treat it
            # as an anonymous caller — is how a tool that exists to filter starts
            # returning everything.
            log.error("mcphub.identified_tool_without_caller", server=self._name, tool=tool)
            return ToolResult.failure(f"{tool!r} requires an identified caller")

        return await handler(arguments, principal)  # type: ignore[call-arg]


class MCPToolSource:
    """Adapts a live MCP client session to :class:`ToolSource`.

    Owns no transport lifecycle: callers pass an initialised session, so stdio,
    streamable HTTP, and test doubles are all handled the same way.
    """

    def __init__(
        self,
        name: str,
        session: Any,
        *,
        risk_overrides: dict[str, RiskClass] | None = None,
    ) -> None:
        self._name = name
        self._session = session
        self._risk_overrides = risk_overrides or {}
        self._pinned: list[ToolSpec] | None = None

    def pin(self, specs: list[ToolSpec]) -> None:
        """Serve exactly these tools from now on.

        Set by the supervisor after the declaration is checked against what was
        approved. Without it, a re-listing would silently readmit a tool that was
        withheld — the catalog is refreshed on reconnect, and a control that only
        holds until the next reconnect is not a control.
        """
        self._pinned = list(specs)

    @property
    def name(self) -> str:
        return self._name

    async def list_tools(self) -> list[ToolSpec]:
        if self._pinned is not None:
            return list(self._pinned)

        result = await self._session.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            description = describe_remote_tool(self._name, tool.name, tool.description or "")
            if description is None:
                # A description carrying injection patterns is not shown to the
                # model at all — the tool is withheld. See `describe_remote_tool`.
                continue
            specs.append(
                ToolSpec(
                    server=self._name,
                    tool=tool.name,
                    description=description,
                    parameters=tool.inputSchema or {"type": "object", "properties": {}},
                    risk=classify_risk(tool.name, tool.annotations, self._risk_overrides),
                    # Anything a third-party server returns is text an outsider
                    # may have authored. Marking it here means reading from a
                    # remote server taints the session, exactly as inbound mail
                    # does, without every connector having to remember.
                    returns_untrusted_content=True,
                )
            )
        return specs

    async def call(
        self, tool: str, arguments: dict[str, Any], *, principal: Principal | None = None
    ) -> ToolResult:
        """Invoke a remote tool.

        ``principal`` is accepted and deliberately not forwarded. A third-party
        MCP server has no notion of our users, and the credential it authenticates
        with is the one the operator configured for the whole server — so passing
        a user id would say "this ran as Alice" when the server saw a service
        account. Per-user credentials against remote servers are feature F3.2 and
        need a credential store, not an extra argument.
        """
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as exc:  # noqa: BLE001 — transport and protocol errors alike
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")

        text = _flatten_content(getattr(result, "content", None))
        structured = getattr(result, "structuredContent", None)

        if getattr(result, "isError", False):
            return ToolResult.failure(text or "tool reported an error")
        return ToolResult.success(text, structured)


def _flatten_content(content: Any) -> str:
    """Render MCP content blocks to text.

    Non-text blocks are summarised rather than dropped: a silent omission would
    let the model reason about a truncated result without knowing it.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", ""))
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            parts.append(getattr(resource, "text", None) or f"[resource {block_type}]")
        elif block_type is not None:
            parts.append(f"[{block_type} content omitted]")
        else:
            parts.append(json.dumps(block, default=str))
    return "\n".join(p for p in parts if p)
