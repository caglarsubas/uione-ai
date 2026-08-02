"""Read-after-write verification — feature F2.6.

An assistant that reports what it *attempted* is a different product from one
that reports what *happened*. Connectors return the vendor's response to the
write, which is the vendor agreeing it received the request — not evidence the
state changed. Between those two lies every "I closed the ticket" that left the
ticket open: a 200 on a field the API silently ignored, a workflow rule that
reverted the transition, a write against the wrong record.

So after a mutating call succeeds, the gateway calls a **read** tool back through
itself and compares what it finds against what was asked for.

Four rules, each of which the obvious implementation gets wrong:

**A contradicted write is not a failed call.** The write executed. Returning it
as a failure invites the model to retry, and retrying a write that already landed
is how one comment becomes two. The result stands; the verdict rides alongside it
and the audit record carries it.

**Unverifiable is not verified.** A tool with no registered plan comes back
``UNVERIFIABLE``, never ``CONFIRMED``. The north-star metric counts confirmed
actions, and a metric that silently counts unchecked ones measures nothing.

**A failed read-back is not a contradiction.** If the system goes down between
the write and the read, that is ``UNAVAILABLE``. Reporting it as "your ticket did
not close" would be a false alarm, and a verifier that cries wolf gets ignored
exactly when it is right.

**Verification never blocks the write.** It is bounded by a timeout and its own
errors are swallowed into a verdict. A slow read must not hold up a reply, and a
bug in a predicate must not lose a write that succeeded.

**Known gap:** only *successful* mutations are verified. The inverse case — a
connector reporting failure on a write that actually landed, after a timeout on
the vendor's side — is the nastier drift and is not covered here. It needs
predicates that assert absence rather than presence, which is a different shape;
naming it beats implying the gate is total.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import structlog

from uione.mcphub import Principal, ToolResult, ToolSpec, VerificationPlan

log = structlog.get_logger(__name__)

#: How long a read-back may take before the write is reported unverified. Short:
#: this is latency added to every write, on the path of a user waiting for a
#: reply, and a system too slow to answer a read is one whose confirmation would
#: be stale anyway.
DEFAULT_TIMEOUT_S = 10.0


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    """Read back, and the world matches what was asked for."""

    CONTRADICTED = "contradicted"
    """Read back, and it does not. The user needs to know; the model must not retry."""

    UNVERIFIABLE = "unverifiable"
    """No plan registered for this tool. Explicitly not a pass."""

    UNAVAILABLE = "unavailable"
    """The read-back could not be completed. Says nothing about the write."""

    @property
    def is_confirmed(self) -> bool:
        return self is Verdict.CONFIRMED


#: ``(arguments, result) -> VerificationPlan | None``. Returning ``None`` means
#: this particular call cannot be checked — a plan builder that cannot parse the
#: arguments says so rather than guessing at a record to re-read.
PlanBuilder = Callable[[dict[str, Any], ToolResult], VerificationPlan | None]


@dataclass(frozen=True)
class Verification:
    """The outcome of checking one write."""

    verdict: Verdict
    detail: str = ""

    @property
    def contradicted(self) -> bool:
        return self.verdict is Verdict.CONTRADICTED

    @property
    def note(self) -> str:
        """What to append to the tool result so the model does not overstate.

        A property on the verdict rather than a helper the gateway calls, because
        the gateway must not import governance — it holds the contract, we hold
        the policy about what a model should be told.

        Only the two bad verdicts spend context. Telling a model a write was
        confirmed invites it to say so at length; saying nothing when all is well
        is the quiet default. The explicit "do not retry" is there because a
        model's instinct on hearing something went wrong is to try again, which
        for a write that already executed is the worst move available.
        """
        if self.verdict is Verdict.CONTRADICTED:
            return (
                f"\n\n[verification] The write executed but reading it back does not "
                f"confirm it: {self.detail}. Report this to the user. "
                f"Do not retry — the action may have partially taken effect."
            )
        if self.verdict is Verdict.UNAVAILABLE:
            return (
                f"\n\n[verification] Could not confirm this took effect "
                f"({self.detail}). Say so rather than asserting success."
            )
        return ""


class ToolCaller(Protocol):
    """How the verifier reaches a read tool: back through the gateway.

    Through the gateway rather than around it, so the read-back is policy-checked
    and audited like any other call. A verification pass that reads systems
    without appearing in the audit log is a hole in the record of what the
    assistant looked at.
    """

    async def __call__(
        self, principal: Principal, tool: str, arguments: dict[str, Any]
    ) -> ToolResult: ...


class ActionVerifier:
    """Registry of read-after-write plans, and the check itself."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._builders: dict[str, PlanBuilder] = {}
        self._timeout_s = timeout_s

    def register(self, tool: str, builder: PlanBuilder) -> None:
        """Teach the verifier how to confirm one tool's writes."""
        self._builders[tool] = builder

    def knows(self, tool: str) -> bool:
        return tool in self._builders

    @property
    def verifiable_tools(self) -> frozenset[str]:
        """Which writes can be confirmed — surfaced so the gap is visible."""
        return frozenset(self._builders)

    async def verify(
        self,
        principal: Principal,
        spec: ToolSpec,
        arguments: dict[str, Any],
        result: ToolResult,
        call: ToolCaller,
    ) -> Verification:
        builder = self._builders.get(spec.qualified_name)
        if builder is None:
            return Verification(Verdict.UNVERIFIABLE, "no read-back is registered for this tool")

        try:
            plan = builder(arguments, result)
        except Exception:  # noqa: BLE001 — a broken predicate must not lose the write
            log.exception("governance.verification_plan_failed", tool=spec.qualified_name)
            return Verification(Verdict.UNAVAILABLE, "could not plan a read-back")

        if plan is None:
            return Verification(Verdict.UNVERIFIABLE, "this call cannot be read back")

        try:
            async with asyncio.timeout(self._timeout_s):
                readback = await call(principal, plan.tool, plan.arguments)
        except TimeoutError:
            log.warning(
                "governance.verification_timed_out", tool=spec.qualified_name, readback=plan.tool
            )
            return Verification(Verdict.UNAVAILABLE, f"{plan.tool} did not answer in time")
        except Exception as exc:  # noqa: BLE001
            log.exception("governance.verification_failed", tool=spec.qualified_name)
            return Verification(Verdict.UNAVAILABLE, f"{type(exc).__name__} reading back")

        if not readback.ok:
            return Verification(
                Verdict.UNAVAILABLE, f"could not read back: {readback.error or 'unknown error'}"
            )

        try:
            matched = plan.expect(readback)
        except Exception:  # noqa: BLE001
            log.exception("governance.verification_predicate_failed", tool=spec.qualified_name)
            return Verification(Verdict.UNAVAILABLE, "could not interpret the read-back")

        if matched is None:
            # The predicate read the answer and it did not settle the question —
            # a truncated list against an absence check, most often. Not a
            # contradiction, and emphatically not a confirmation.
            return Verification(
                Verdict.UNAVAILABLE,
                f"the read-back could not establish that {plan.describes or 'the change landed'}",
            )

        if matched:
            log.info(
                "governance.write_confirmed",
                principal=principal.user_id,
                tool=spec.qualified_name,
                expected=plan.describes,
            )
            return Verification(Verdict.CONFIRMED, plan.describes)

        # The loud one. This is the case the feature exists for: the connector
        # said yes and the system disagrees.
        log.error(
            "governance.write_contradicted",
            principal=principal.user_id,
            tool=spec.qualified_name,
            expected=plan.describes,
            readback=plan.tool,
        )
        return Verification(
            Verdict.CONTRADICTED,
            f"expected {plan.describes or 'the change'}, and reading it back shows otherwise",
        )
