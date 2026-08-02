"""Core types for the MCP hub."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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

    Every tool call carries one, which is what makes the audit trail attributable
    and what retrieval filters by. It is passed to the handler, never stashed on
    the source between calls — see :mod:`uione.mcphub.source`.

    **This is not yet F3.2.** The principal governs what our side permits; the
    credential the connector then authenticates with is still one service account
    per system, configured by the operator. So the source system sees one identity
    for everybody, and its own permissions cannot distinguish our users. Binding a
    per-user credential here is the remaining half, and it needs a credential
    store rather than another field.
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


@dataclass(frozen=True)
class VerificationPlan:
    """How to read one mutating call back and check it landed (F2.6).

    Lives here rather than in :mod:`uione.governance` because connectors are what
    construct it, and a connector importing the governance plane to describe its
    own tools would invert the layering. Governance owns the *policy* — when to
    verify, what a verdict means, what the model is told — and this is only the
    vocabulary: a tool to call, arguments to call it with, and a question to ask
    of the answer.

    ``expect`` reads the *read-back* result, never the write's own response.
    Comparing a write to what it said about itself verifies nothing: it is the
    same claim, made twice.
    """

    tool: str
    """The read tool to call back. A READ — anything else would mutate again."""

    arguments: dict[str, Any]

    expect: Callable[[ToolResult], bool | None]
    """True confirms, False contradicts, and **None means the read-back could not
    settle it**.

    The third case is not squeamishness. A check that asserts an *absence* — "this
    message is no longer in the unread list" — is only valid against a complete
    list, and a truncated one cannot distinguish "gone" from "past the ceiling".
    Forced to return a bool, such a predicate must either invent a confirmation it
    did not earn or raise a false alarm. ``None`` is the honest third answer, and
    it maps to ``unavailable``.
    """

    describes: str = ""
    """What was expected, in a user's words: "uione/payments#3 is closed"."""


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
