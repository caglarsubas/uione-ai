"""Core types for the MCP hub."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from uione.modelplane.types import ToolDefinition


class RiskClass(StrEnum):
    """How much damage a tool can do.

    Every tool carries one. The approval ladder (gap G1) reads it to decide what
    may run unattended, and the audit log records it so an auditor can ask "what
    irreversible things happened last week?" without reading every entry.
    """

    READ = "read"
    """No state change. Still audited — reads can leak."""

    REVERSIBLE_WRITE = "reversible_write"
    """Changes state, and a compensating action exists (reopen, revert status)."""

    IRREVERSIBLE = "irreversible"
    """Cannot be undone: deletions, payments, permanent state transitions."""

    EXTERNAL_FACING = "external_facing"
    """Leaves the organisation: sends mail, posts publicly, uploads outward.

    Ranked separately from irreversible because the blast radius is reputational
    rather than technical, and because it is the target of exfiltration attacks.
    """


#: Risk classes that mutate something. Everything here needs governance.
MUTATING_RISKS = frozenset(
    {RiskClass.REVERSIBLE_WRITE, RiskClass.IRREVERSIBLE, RiskClass.EXTERNAL_FACING}
)


class Principal(BaseModel):
    """Who is acting.

    The agent always acts *as a user*, never as a shared service account — that is
    what makes source-system permissions meaningful and the audit trail
    attributable (feature F3.2).
    """

    user_id: str
    roles: frozenset[str] = Field(default_factory=frozenset)
    display_name: str = ""

    model_config = {"frozen": True}

    def __str__(self) -> str:
        return self.display_name or self.user_id


class ToolSpec(BaseModel):
    """A tool as the hub knows it, after namespacing and risk classification."""

    server: str
    tool: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    risk: RiskClass = RiskClass.READ

    returns_untrusted_content: bool = False
    """Whether this tool surfaces text an outsider could have authored.

    Set by the connector: inbound mail, public ticket comments and chat from
    external guests are all ``True``. Reading such a tool taints the session, so
    subsequent write actions need a human even if the user had earned autonomy.
    """

    model_config = {"frozen": True}

    @property
    def qualified_name(self) -> str:
        """Server-qualified name.

        Namespacing is not cosmetic: two connectors will both offer ``search``,
        and a model that picks the wrong one searches the wrong system.
        """
        return f"{self.server}.{self.tool}"

    @property
    def mutating(self) -> bool:
        return self.risk in MUTATING_RISKS

    def to_tool_definition(self) -> ToolDefinition:
        """Render for the model. Models see qualified names only."""
        return ToolDefinition(
            name=self.qualified_name,
            description=self.description,
            parameters=self.parameters,
        )


class ActionContext(BaseModel):
    """Session state that governance needs in order to judge an action.

    Carries taint: whether untrusted content has entered this run's context. An
    action requested while an attacker's text is in the context window is not the
    same action requested from a clean session, even with identical arguments.
    """

    tainted: bool = False
    taint_summary: str = ""
    correlation_id: str | None = None
    approved_action_id: str | None = None
    """Set when executing a previously approved action, so it is not re-held."""

    model_config = {"frozen": True}


class ToolResult(BaseModel):
    """Outcome of one tool invocation."""

    ok: bool
    content: str = ""
    structured: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def success(cls, content: str, structured: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=True, content=content, structured=structured)

    @classmethod
    def failure(cls, error: str) -> ToolResult:
        return cls(ok=False, error=error)
