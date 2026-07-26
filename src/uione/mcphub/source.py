"""Tool sources — where tools actually come from.

The gateway talks to this protocol, never to a transport. That keeps all the
governance logic testable without spawning servers, and keeps MCP version churn
contained to one file.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

from uione.mcphub.types import RiskClass, ToolResult, ToolSpec

log = structlog.get_logger(__name__)


class ToolSource(Protocol):
    """A named collection of callable tools."""

    @property
    def name(self) -> str: ...

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult: ...


def classify_risk(
    tool_name: str,
    annotations: Any | None,
    overrides: dict[str, RiskClass] | None = None,
) -> RiskClass:
    """Assign a risk class to a discovered tool.

    Order of authority: our explicit override, then the server's own MCP
    annotations, then the safe default.

    The safe default is ``IRREVERSIBLE``, not ``READ``. An unclassified tool is a
    tool nobody has reasoned about, and mistaking a destructive one for a read is
    the expensive direction of that error. Connector certification (F3.8) requires
    explicit classification, so hitting this default in production means a
    connector shipped without review.
    """
    overrides = overrides or {}
    if tool_name in overrides:
        return overrides[tool_name]

    if annotations is not None:
        if getattr(annotations, "readOnlyHint", None) is True:
            return RiskClass.READ
        if getattr(annotations, "destructiveHint", None) is True:
            return RiskClass.IRREVERSIBLE
        if getattr(annotations, "openWorldHint", None) is True:
            return RiskClass.EXTERNAL_FACING
        if getattr(annotations, "idempotentHint", None) is True:
            return RiskClass.REVERSIBLE_WRITE

    log.warning("mcphub.unclassified_tool", tool=tool_name, assigned=str(RiskClass.IRREVERSIBLE))
    return RiskClass.IRREVERSIBLE


class InMemoryToolSource:
    """A tool source backed by Python callables.

    Used by tests and by built-in tools that need no external process.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]] = {}

    @property
    def name(self) -> str:
        return self._name

    def register(
        self,
        tool: str,
        handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        risk: RiskClass = RiskClass.READ,
        returns_untrusted_content: bool = False,
    ) -> ToolSpec:
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
        return spec

    async def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(tool)
        if handler is None:
            return ToolResult.failure(f"unknown tool {tool!r} on server {self._name!r}")
        return await handler(arguments)


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

    @property
    def name(self) -> str:
        return self._name

    async def list_tools(self) -> list[ToolSpec]:
        result = await self._session.list_tools()
        specs: list[ToolSpec] = []
        for tool in result.tools:
            specs.append(
                ToolSpec(
                    server=self._name,
                    tool=tool.name,
                    description=tool.description or "",
                    parameters=tool.inputSchema or {"type": "object", "properties": {}},
                    risk=classify_risk(tool.name, tool.annotations, self._risk_overrides),
                )
            )
        return specs

    async def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
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
