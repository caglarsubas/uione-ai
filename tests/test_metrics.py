"""The Prometheus endpoint, and the two things it must never do:

serve without a token, and say who did what.
"""

from __future__ import annotations

import pytest

from uione.governance import ActionVerifier, Verdict
from uione.mcphub import (
    AuditLog,
    AuditOutcome,
    AuditRecord,
    FanOutAuditSink,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
    ToolResult,
    VerificationPlan,
)
from uione.observability import MetricsAuditSink, MetricsRegistry

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
BOB = Principal(user_id="bob", roles=frozenset({"analyst"}))

POLICY = ToolPolicy(
    [Grant(role="analyst", tools=frozenset({"widgets.*"}), max_risk=RiskClass.IRREVERSIBLE)]
)


def series(rendered: str, name: str) -> dict[str, float]:
    """Parse the exposition back into {labels: value} for one metric name."""
    found = {}
    for line in rendered.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        key, _, value = line.rpartition(" ")
        if key == name or key.startswith(f"{name}{{"):
            found[key[len(name) :]] = float(value)
    return found


# -- the exposition format -------------------------------------------------


def test_the_format_is_parseable_and_typed() -> None:
    registry = MetricsRegistry()
    registry.tool_calls.inc(server="mail", tool="mail.search", risk="read", outcome="allowed")

    rendered = registry.render()

    assert "# TYPE uione_tool_calls_total counter" in rendered
    assert "# HELP uione_tool_calls_total" in rendered
    assert rendered.endswith("\n")
    assert (
        'uione_tool_calls_total{outcome="allowed",risk="read",server="mail",tool="mail.search"} 1'
        in rendered
    )


def test_label_values_are_escaped() -> None:
    """A tool name with a quote in it must not produce unparseable output."""
    registry = MetricsRegistry()
    registry.tool_calls.inc(server='we"ird', tool="t", risk="read", outcome="allowed")

    assert r'server="we\"ird"' in registry.render()


def test_a_summary_carries_count_and_sum() -> None:
    registry = MetricsRegistry()
    registry.tool_duration.observe(0.5, server="mail")
    registry.tool_duration.observe(1.5, server="mail")

    rendered = registry.render()

    assert 'uione_tool_call_duration_seconds_count{server="mail"} 2' in rendered
    assert 'uione_tool_call_duration_seconds_sum{server="mail"} 2' in rendered


# -- fed from the audit stream ---------------------------------------------


async def build(registry: MetricsRegistry, *, obedient: bool = True) -> McpGateway:
    source = InMemoryToolSource("widgets")
    stored: dict = {}

    async def set_value(args: dict) -> ToolResult:
        if obedient:
            stored["value"] = args.get("value")
        return ToolResult.success("done")

    async def get_value(_args: dict) -> ToolResult:
        return ToolResult.success("read", {"value": stored.get("value")})

    source.register("set_value", set_value, risk=RiskClass.REVERSIBLE_WRITE)
    source.register("get_value", get_value, risk=RiskClass.READ)

    verifier = ActionVerifier()
    verifier.register(
        "widgets.set_value",
        lambda args, _r: VerificationPlan(
            tool="widgets.get_value",
            arguments={},
            expect=lambda r: (r.structured or {}).get("value") == args.get("value"),
            describes="the value",
        ),
    )
    gateway = McpGateway(
        policy=POLICY,
        audit=AuditLog(FanOutAuditSink(InMemoryAuditSink(), MetricsAuditSink(registry))),
        verifier=verifier,
    )
    await gateway.register(source)
    return gateway


async def test_a_confirmed_write_counts_toward_the_north_star() -> None:
    registry = MetricsRegistry()
    gateway = await build(registry)

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    rendered = registry.render()
    assert series(rendered, "uione_verified_actions_total") == {
        '{server="widgets",tool="widgets.set_value"}': 1
    }
    assert series(rendered, "uione_mutating_actions_total") == {
        '{server="widgets",tool="widgets.set_value"}': 1
    }


async def test_a_contradicted_write_counts_as_a_mutation_but_not_a_confirmation() -> None:
    """It executed, so it is in the denominator. It was not confirmed, so it is
    not in the numerator — which is what makes the ratio mean anything."""
    registry = MetricsRegistry()
    gateway = await build(registry, obedient=False)

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    rendered = registry.render()
    assert not series(rendered, "uione_verified_actions_total")
    assert series(rendered, "uione_mutating_actions_total") == {
        '{server="widgets",tool="widgets.set_value"}': 1
    }
    assert series(rendered, "uione_unconfirmed_actions_total") == {
        '{server="widgets",tool="widgets.set_value"}': 1
    }


async def test_reads_are_counted_but_are_not_mutations() -> None:
    registry = MetricsRegistry()
    gateway = await build(registry)

    await gateway.call(ALICE, "widgets.get_value", {})

    rendered = registry.render()
    assert series(rendered, "uione_tool_calls_total")
    assert not series(rendered, "uione_mutating_actions_total")


def test_no_series_is_labelled_by_user() -> None:
    """G15: admins get aggregate analytics, not a per-employee breakdown.

    A Prometheus query must not be able to answer "who used the assistant least
    this week". That belongs to the audit log, which is access-controlled.
    """
    registry = MetricsRegistry()
    for principal in (ALICE, BOB):
        registry.observe_audit(
            AuditRecord(
                principal_id=principal.user_id,
                tool="widgets.set_value",
                server="widgets",
                risk=RiskClass.REVERSIBLE_WRITE,
                outcome=AuditOutcome.ALLOWED,
                arguments_hash="x",
                verification=str(Verdict.CONFIRMED),
            )
        )

    rendered = registry.render()

    assert "alice" not in rendered
    assert "bob" not in rendered
    assert "principal" not in rendered
    # Both were still counted — aggregate, not absent.
    assert series(rendered, "uione_verified_actions_total") == {
        '{server="widgets",tool="widgets.set_value"}': 2
    }


def test_token_totals_are_set_not_accumulated() -> None:
    """The recorder already accumulates. Adding its running totals every scrape
    would multiply them, and the graph would look like exponential growth."""
    from uione.modelplane.types import Usage

    registry = MetricsRegistry()
    by_model = {"ministral-3:8b": Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)}

    registry.observe_usage(by_model, calls=1)
    registry.observe_usage(by_model, calls=1)

    assert series(registry.render(), "uione_model_tokens_total") == {
        '{kind="prompt",model="ministral-3:8b"}': 100,
        '{kind="completion",model="ministral-3:8b"}': 20,
    }


# -- the endpoint ----------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from uione.api.app import create_app
    from uione.config import get_settings

    def make(token: str):
        monkeypatch.setenv("UIONE_METRICS_TOKEN", token)
        monkeypatch.setenv("UIONE_AUTH_MODE", "dev")
        monkeypatch.setenv("UIONE_ENVIRONMENT", "dev")
        get_settings.cache_clear()
        return TestClient(create_app())

    yield make
    get_settings.cache_clear()


def test_an_unconfigured_deployment_has_no_endpoint(client) -> None:
    """404 rather than 401: a deployment that never enabled metrics should not
    advertise that the endpoint exists."""
    with client("") as c:
        assert c.get("/metrics").status_code == 404


def test_scraping_without_the_token_is_refused(client) -> None:
    with client("s3cret") as c:
        response = c.get("/metrics")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_wrong_token_is_refused(client) -> None:
    with client("s3cret") as c:
        assert c.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_a_valid_scrape_returns_the_exposition(client) -> None:
    with client("s3cret") as c:
        response = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "# TYPE uione_tool_calls_total counter" in response.text
    # Gauges are read at scrape time, so a fresh process still reports connectors.
    assert "uione_connector_up" in response.text
