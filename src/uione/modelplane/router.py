"""Task-class routing.

Callers name the *operation* they are performing; the router decides which model
tier serves it. Keeping that decision in one table (rather than scattered at call
sites) is what makes the cost profile of the whole product auditable, and what
lets a deployment re-tier everything by editing configuration (gap G11).
"""

from __future__ import annotations

import structlog

from uione.config import Settings, get_settings
from uione.modelplane.types import TaskClass

log = structlog.get_logger(__name__)

# Operation -> tier. The bias is deliberate: default to the cheapest tier that can
# plausibly do the job, and let escalation handle the exceptions.
DEFAULT_ROUTES: dict[str, TaskClass] = {
    # Triage: short, structured, high-volume.
    "classify": TaskClass.TRIAGE,
    "extract": TaskClass.TRIAGE,
    "route": TaskClass.TRIAGE,
    "detect_pii": TaskClass.TRIAGE,
    "rank_relevance": TaskClass.TRIAGE,
    "triage_message": TaskClass.TRIAGE,
    # Workhorse: most user-visible generation and tool use.
    "summarize": TaskClass.WORKHORSE,
    "draft_reply": TaskClass.WORKHORSE,
    "draft_document": TaskClass.WORKHORSE,
    "answer_question": TaskClass.WORKHORSE,
    "tool_call": TaskClass.WORKHORSE,
    "verify_action": TaskClass.WORKHORSE,
    # Reasoning: multi-step, cross-system, expensive.
    "plan": TaskClass.REASONING,
    "compose_brief": TaskClass.REASONING,
    "synthesize_report": TaskClass.REASONING,
    "analyze_anomaly": TaskClass.REASONING,
    "resolve_entities": TaskClass.REASONING,
}

_ESCALATION: dict[TaskClass, TaskClass | None] = {
    TaskClass.TRIAGE: TaskClass.WORKHORSE,
    TaskClass.WORKHORSE: TaskClass.REASONING,
    TaskClass.REASONING: None,
}


class UnknownOperationError(KeyError):
    """Raised for an unrouted operation name.

    Failing loudly beats silently defaulting: an unrouted operation is usually a
    typo, and silently sending it to the expensive tier is how GPU bills grow
    without anyone noticing.
    """


class TaskRouter:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        routes: dict[str, TaskClass] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._routes = dict(routes or DEFAULT_ROUTES)

    def route(self, operation: str) -> TaskClass:
        try:
            return self._routes[operation]
        except KeyError:
            raise UnknownOperationError(
                f"no route for operation {operation!r}; "
                f"add it to DEFAULT_ROUTES or pass an explicit task class"
            ) from None

    def model_for(self, operation: str) -> str:
        return self.model_for_tier(self.route(operation))

    def model_for_tier(self, task: TaskClass) -> str:
        return {
            TaskClass.TRIAGE: self._settings.model_tier_triage,
            TaskClass.WORKHORSE: self._settings.model_tier_workhorse,
            TaskClass.REASONING: self._settings.model_tier_reasoning,
        }[task]

    def escalate(self, task: TaskClass) -> TaskClass | None:
        """Next tier up, or ``None`` if already at the top.

        Used when a cheap tier fails a schema or verification check: retrying the
        same prompt on a stronger model is usually cheaper than failing the user's
        request outright.
        """
        return _ESCALATION[task]

    def register(self, operation: str, task: TaskClass) -> None:
        self._routes[operation] = task

    def describe(self) -> dict[str, dict[str, str]]:
        """Flatten the routing table for the admin console."""
        return {
            operation: {"tier": str(task), "model": self.model_for_tier(task)}
            for operation, task in sorted(self._routes.items())
        }
