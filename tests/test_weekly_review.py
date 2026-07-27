"""The daily census and the weekly review.

The census is the missing half of the anomaly detector: connectors answer "what
is true now", and now compared against nothing is just a number. The review is
what that history is for — and the reason it exists as its own generator is that
the previous "weekly review" ran the morning brief with a different greeting,
which promises a different kind of thinking and delivers the same unread mail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from uione.analysis import Detector, MetricRecorder, MetricSource, Point
from uione.config import Settings
from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
    ToolResult,
)
from uione.modelplane import Completion, ModelPlaneUnavailable
from uione.proactive import Movement, WeeklyReviewGenerator, compare_weeks
from uione.storage import Database, MetricStore

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


class StubModel:
    def __init__(self, content: str = "Your week.") -> None:
        self.content = content
        self.prompts: list[str] = []

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[-1].content or "")
        return Completion(content=self.content, model="stub")


class DeadModel:
    async def chat(self, messages, **kwargs):
        raise ModelPlaneUnavailable("engine down")


@pytest.fixture
async def database(tmp_path):
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"))
    await db.create_schema()
    yield db
    await db.dispose()


@pytest.fixture
def store(database: Database) -> MetricStore:
    return MetricStore(database)


async def build_gateway(counts: dict[str, int], *, failing: set[str] = frozenset()) -> McpGateway:
    """A gateway whose tools return the counts a census reads."""
    gateway = McpGateway(
        policy=ToolPolicy(
            [
                Grant(
                    role="analyst",
                    tools=frozenset(f"{q.split('.')[0]}.*" for q in counts),
                    max_risk=RiskClass.READ,
                )
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )
    for qualified, count in counts.items():
        server, _, tool = qualified.partition(".")
        source = InMemoryToolSource(server)

        def handler(_args: dict, count: int = count, server: str = server) -> ToolResult:
            if server in failing:
                return ToolResult.failure("connector unavailable")
            return ToolResult.success(f"{count} things", {"count": count})

        async def call(args: dict, handler=handler) -> ToolResult:
            return handler(args)

        source.register(tool, call, description=tool, risk=RiskClass.READ)
        await gateway.register(source)
    return gateway


# -- the census ------------------------------------------------------------


async def test_the_census_records_what_this_deployment_has(store: MetricStore) -> None:
    gateway = await build_gateway({"incidents.my_incidents": 4, "tasks.my_open_issues": 9})
    recorder = MetricRecorder(gateway, store=store)

    snapshot = await recorder.take(ALICE, now=NOW)

    assert snapshot.values == {"open_incidents": 4.0, "open_tasks": 9.0}
    assert snapshot.complete


async def test_a_metric_whose_tool_is_absent_is_not_recorded(store: MetricStore) -> None:
    """Absent is not zero — the same distinction the brief makes between a
    system that is down and one that was never installed."""
    gateway = await build_gateway({"incidents.my_incidents": 4})
    recorder = MetricRecorder(gateway, store=store)

    snapshot = await recorder.take(ALICE, now=NOW)

    assert "open_claims" not in snapshot.values
    assert "open_claims" not in snapshot.unavailable


async def test_a_failing_connector_records_a_gap_not_a_zero(store: MetricStore) -> None:
    """The one that matters. A 0 creates a cliff in the series, the detector
    fires on it, and somebody is told their incidents vanished overnight."""
    gateway = await build_gateway(
        {"incidents.my_incidents": 4, "tasks.my_open_issues": 9}, failing={"incidents"}
    )
    recorder = MetricRecorder(gateway, store=store)

    snapshot = await recorder.take(ALICE, now=NOW)

    assert "open_incidents" not in snapshot.values
    assert "open_incidents" in snapshot.unavailable
    assert not snapshot.complete


async def test_a_non_numeric_result_is_not_recorded(store: MetricStore) -> None:
    gateway = McpGateway(
        policy=ToolPolicy(
            [Grant(role="analyst", tools=frozenset({"incidents.*"}), max_risk=RiskClass.READ)]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )
    source = InMemoryToolSource("incidents")

    async def odd(_args: dict) -> ToolResult:
        return ToolResult.success("some", {"count": "four"})

    source.register("my_incidents", odd, description="x", risk=RiskClass.READ)
    await gateway.register(source)

    snapshot = await MetricRecorder(gateway, store=store).take(ALICE, now=NOW)

    assert "open_incidents" in snapshot.unavailable


async def test_a_boolean_is_not_mistaken_for_a_number(store: MetricStore) -> None:
    """`isinstance(True, int)` is True in Python, so a tool returning a flag
    named `count` would otherwise be recorded as 1.0 every day."""
    gateway = McpGateway(
        policy=ToolPolicy(
            [Grant(role="analyst", tools=frozenset({"incidents.*"}), max_risk=RiskClass.READ)]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )
    source = InMemoryToolSource("incidents")

    async def flag(_args: dict) -> ToolResult:
        return ToolResult.success("yes", {"count": True})

    source.register("my_incidents", flag, description="x", risk=RiskClass.READ)
    await gateway.register(source)

    snapshot = await MetricRecorder(gateway, store=store).take(ALICE, now=NOW)

    assert "open_incidents" in snapshot.unavailable


async def test_the_census_survives_a_restart(store: MetricStore, database: Database) -> None:
    gateway = await build_gateway({"incidents.my_incidents": 4})
    await MetricRecorder(gateway, store=store).take(ALICE, now=NOW)

    reopened = MetricStore(database)
    history = await reopened.history("alice")

    assert history["open_incidents"][0].value == 4.0


async def test_two_ticks_on_one_day_do_not_double_count(store: MetricStore) -> None:
    """A restart or a retried tick must overwrite. Two entries for one Tuesday
    would quietly skew the detector's weekday baseline, and nothing would ever
    surface it."""
    gateway = await build_gateway({"incidents.my_incidents": 4})
    recorder = MetricRecorder(gateway, store=store)

    await recorder.take(ALICE, now=NOW)
    await recorder.take(ALICE, now=NOW + timedelta(hours=3))

    assert len((await store.history("alice"))["open_incidents"]) == 1


# -- comparing weeks -------------------------------------------------------


def _series(values: list[float], *, end: datetime = NOW) -> list[Point]:
    return [
        Point(at=end - timedelta(days=len(values) - 1 - i), value=v) for i, v in enumerate(values)
    ]


def test_a_week_is_compared_by_average_not_by_endpoints() -> None:
    """Comparing today with the number exactly seven days ago makes the review
    depend on which two days those happened to be. One bank holiday and every
    metric looks like a crisis."""
    # Last week averages 10, this week averages 20 — but the endpoints alone
    # would say 12 vs 20.
    series = {"open_tasks": _series([10, 10, 10, 10, 10, 10, 12, 20, 20, 20, 20, 20, 20, 20])}

    movements = compare_weeks(series, now=NOW)

    assert movements[0].previous == pytest.approx(10.3, abs=0.5)
    assert movements[0].current == pytest.approx(20.0, abs=0.5)


def test_a_metric_with_only_this_week_is_not_compared() -> None:
    """Inventing a comparison from one week would be the most confident number
    in the report."""
    movements = compare_weeks({"new_metric": _series([5, 6, 7])}, now=NOW)

    assert movements == []


def test_the_largest_movement_leads() -> None:
    series = {
        "small": _series([1] * 7 + [2] * 7),
        "large": _series([1] * 7 + [40] * 7),
    }

    movements = compare_weeks(series, now=NOW)

    assert movements[0].metric == "large"


def test_a_percentage_is_omitted_when_last_week_was_zero() -> None:
    """ "Up 100% from 0" is a division nobody meant and reads as precision."""
    movement = Movement(metric="m", title="m", current=5, previous=0, days_observed=7)

    assert "%" not in movement.render()


# -- the review ------------------------------------------------------------


async def test_a_deployment_with_no_history_says_so(store: MetricStore) -> None:
    """A review that pretends is the first one and the last one anybody opens."""
    generator = WeeklyReviewGenerator(model=StubModel(), store=store)

    review = await generator.generate(ALICE, now=NOW)

    assert "No history yet" in review.body
    assert not review.has_history


async def test_the_model_is_given_figures_it_cannot_recalculate(store: MetricStore) -> None:
    """Asking a model to compare this week's count with last week's is asking
    for a plausible number. It writes the prose; it never does the sums."""
    for day in range(14, 0, -1):
        await store.record(
            "alice", {"open_tasks": 10.0 if day > 7 else 30.0}, at=NOW - timedelta(days=day)
        )
    model = StubModel()

    await WeeklyReviewGenerator(model=model, store=store).generate(ALICE, now=NOW)

    prompt = model.prompts[0]
    assert "30" in prompt and "10" in prompt
    assert "already been computed" not in prompt  # that lives in the system prompt


async def test_the_two_comparisons_are_labelled_as_different_questions(
    store: MetricStore,
) -> None:
    """Watched against a real model, unlabelled blocks produced "dropped 57%
    week over week (3 vs 7)" — where 3 and 7 came from the *other* comparison.
    Both numbers were real; the sentence was not.
    """
    for day in range(20, 0, -1):
        await store.record("alice", {"open_tasks": 10.0}, at=NOW - timedelta(days=day))
    model = StubModel()

    await WeeklyReviewGenerator(model=model, store=store).generate(ALICE, now=NOW)

    prompt = model.prompts[0]
    assert "COMPARISON A" in prompt
    assert "COMPARISON B" in prompt
    assert "one day" in prompt


async def test_an_anomaly_reaches_the_report(store: MetricStore) -> None:
    for day in range(20, 0, -1):
        await store.record("alice", {"open_incidents": 3.0}, at=NOW - timedelta(days=day))
    await store.record("alice", {"open_incidents": 60.0}, at=NOW)

    review = await WeeklyReviewGenerator(
        model=StubModel(), store=store, detector=Detector()
    ).generate(ALICE, now=NOW)

    assert [f.metric for f in review.anomalies] == ["open_incidents"]


async def test_an_anomaly_names_the_day_rather_than_leaving_it_to_be_guessed(
    store: MetricStore,
) -> None:
    """A real model turned "the same weekday baseline" into "the drop on
    Friday" — a confident invention about a day never mentioned."""
    for day in range(20, 0, -1):
        await store.record("alice", {"open_incidents": 3.0}, at=NOW - timedelta(days=day))
    await store.record("alice", {"open_incidents": 60.0}, at=NOW)
    model = StubModel()

    await WeeklyReviewGenerator(model=model, store=store).generate(ALICE, now=NOW)

    assert NOW.strftime("%A") in model.prompts[0]


async def test_metrics_with_too_little_history_are_named(store: MetricStore) -> None:
    """ "Nothing unusual" over three metrics while eight were skipped is the
    sentence that ends trust the day somebody checks."""
    for day in range(20, 0, -1):
        await store.record("alice", {"open_tasks": 10.0}, at=NOW - timedelta(days=day))
    await store.record("alice", {"brand_new": 1.0}, at=NOW)
    model = StubModel()

    review = await WeeklyReviewGenerator(model=model, store=store).generate(ALICE, now=NOW)

    assert "brand_new" in review.unassessed
    assert "Not assessed" in model.prompts[0]


async def test_losing_the_model_keeps_the_figures(store: MetricStore) -> None:
    """The figures are the report. Losing the prose is a degradation; losing the
    numbers would be a failure."""
    for day in range(14, 0, -1):
        await store.record(
            "alice", {"open_tasks": 10.0 if day > 7 else 30.0}, at=NOW - timedelta(days=day)
        )

    review = await WeeklyReviewGenerator(model=DeadModel(), store=store).generate(ALICE, now=NOW)

    assert review.error
    assert "open tasks" in review.body
    assert review.movements


async def test_metric_titles_reach_the_report(store: MetricStore) -> None:
    for day in range(14, 0, -1):
        await store.record("alice", {"open_tasks": 5.0}, at=NOW - timedelta(days=day))

    review = await WeeklyReviewGenerator(
        model=StubModel(), store=store, titles={"open_tasks": "open tasks"}
    ).generate(ALICE, now=NOW)

    assert review.movements[0].title == "open tasks"


def test_a_metric_source_names_its_structured_key() -> None:
    """Reading a count out of prose with a regular expression would make the
    metric a hostage to rendering changes."""
    source = MetricSource("open_incidents", "incidents.my_incidents")

    assert source.key == "count"
