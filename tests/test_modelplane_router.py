from __future__ import annotations

import pytest

from uione.config import Settings
from uione.modelplane import TaskClass, TaskRouter
from uione.modelplane.router import DEFAULT_ROUTES, UnknownOperationError


@pytest.fixture
def router() -> TaskRouter:
    return TaskRouter(
        Settings(
            model_tier_triage="small-model",
            model_tier_workhorse="mid-model",
            model_tier_reasoning="big-model",
        )
    )


def test_high_volume_operations_route_to_the_cheap_tier(router: TaskRouter) -> None:
    for operation in ("classify", "extract", "triage_message", "detect_pii"):
        assert router.route(operation) is TaskClass.TRIAGE


def test_planning_routes_to_the_reasoning_tier(router: TaskRouter) -> None:
    assert router.route("plan") is TaskClass.REASONING
    assert router.route("compose_brief") is TaskClass.REASONING


def test_model_for_resolves_through_the_tier(router: TaskRouter) -> None:
    assert router.model_for("classify") == "small-model"
    assert router.model_for("draft_reply") == "mid-model"
    assert router.model_for("plan") == "big-model"


def test_unknown_operation_fails_loudly(router: TaskRouter) -> None:
    """Silently defaulting an unrouted operation to the expensive tier hides cost."""
    with pytest.raises(UnknownOperationError):
        router.route("summrize")


def test_escalation_walks_up_one_tier(router: TaskRouter) -> None:
    assert router.escalate(TaskClass.TRIAGE) is TaskClass.WORKHORSE
    assert router.escalate(TaskClass.WORKHORSE) is TaskClass.REASONING


def test_escalation_terminates_at_the_top(router: TaskRouter) -> None:
    assert router.escalate(TaskClass.REASONING) is None


def test_routes_can_be_overridden_per_deployment(router: TaskRouter) -> None:
    router.register("summarize", TaskClass.REASONING)
    assert router.model_for("summarize") == "big-model"


def test_describe_exposes_the_whole_table(router: TaskRouter) -> None:
    described = router.describe()
    assert described["plan"] == {"tier": "reasoning", "model": "big-model"}
    assert len(described) == len(DEFAULT_ROUTES)


def test_most_operations_avoid_the_expensive_tier() -> None:
    """Cost posture check: the reasoning tier is the exception, not the default."""
    reasoning = [op for op, tier in DEFAULT_ROUTES.items() if tier is TaskClass.REASONING]
    assert len(reasoning) < len(DEFAULT_ROUTES) / 2
