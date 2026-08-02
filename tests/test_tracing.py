"""Tracing — F11.1.

Two things have to hold: spans carry enough to answer "why was this slow", and
they carry nothing that says who asked.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from uione.governance import ActionVerifier
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
    VerificationPlan,
)
from uione.observability import tracing

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
POLICY = ToolPolicy(
    [Grant(role="analyst", tools=frozenset({"widgets.*"}), max_risk=RiskClass.IRREVERSIBLE)]
)


@pytest.fixture
def spans(monkeypatch) -> InMemorySpanExporter:
    """A tracer writing to memory, installed without touching global OTel state."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("test"))
    yield exporter
    tracing.reset()


def attributes_of(exporter: InMemorySpanExporter, name_prefix: str) -> dict:
    for span in exporter.get_finished_spans():
        if span.name.startswith(name_prefix):
            return dict(span.attributes or {})
    raise AssertionError(
        f"no span starting {name_prefix!r}; saw {[s.name for s in exporter.get_finished_spans()]}"
    )


def build_widgets(*, obedient: bool = True) -> InMemoryToolSource:
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
    return source


async def build(*, obedient: bool = True) -> McpGateway:
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
    gateway = McpGateway(policy=POLICY, audit=AuditLog(InMemoryAuditSink()), verifier=verifier)
    await gateway.register(build_widgets(obedient=obedient))
    return gateway


# -- the no-op path, which is the default install --------------------------


async def test_everything_works_with_no_tracer_configured() -> None:
    """The base install carries no OTel. Spans must cost nothing and break nothing."""
    tracing.reset()
    gateway = await build()

    call = await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert call.ok
    assert call.confirmed


def test_configure_reports_false_without_an_endpoint() -> None:
    tracing.reset()
    assert tracing.configure(endpoint="") is False


def test_a_span_context_yields_none_when_off() -> None:
    tracing.reset()
    with tracing.span("anything", key="value") as current:
        assert current is None
    # Annotating nothing is a no-op rather than an AttributeError.
    tracing.annotate(None, key="value")


# -- the instrumented path -------------------------------------------------


async def test_a_tool_call_produces_a_span_with_its_outcome(spans) -> None:
    gateway = await build()

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    attributes = attributes_of(spans, "tool widgets.set_value")
    assert attributes["uione.server"] == "widgets"
    assert attributes["uione.risk"] == "reversible_write"
    assert attributes["uione.mutating"] is True
    assert attributes["uione.outcome"] == "allowed"
    assert attributes["uione.verification"] == "confirmed"


async def test_a_contradicted_write_says_so_on_the_span(spans) -> None:
    gateway = await build(obedient=False)

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    attributes = attributes_of(spans, "tool widgets.set_value")
    assert attributes["uione.verification"] == "contradicted"
    assert attributes["uione.outcome"] == "unconfirmed"


async def test_the_read_back_nests_under_the_write_it_verifies(spans) -> None:
    """The point of tracing over metrics: one subtree per tool, slow child visible."""
    gateway = await build()

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    finished = {s.name: s for s in spans.get_finished_spans()}
    write = finished["tool widgets.set_value"]
    readback = finished["tool widgets.get_value"]

    assert readback.parent is not None
    assert readback.parent.span_id == write.context.span_id


async def test_a_failing_connector_records_the_failure_on_the_span(spans) -> None:
    source = InMemoryToolSource("widgets")

    async def explode(_args: dict) -> ToolResult:
        raise RuntimeError("connector fell over")

    source.register("set_value", explode, risk=RiskClass.REVERSIBLE_WRITE)
    gateway = McpGateway(policy=POLICY, audit=AuditLog(InMemoryAuditSink()))
    await gateway.register(source)

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    assert attributes_of(spans, "tool widgets.set_value")["uione.outcome"] == "failed"


async def test_no_span_carries_a_user_identifier(spans) -> None:
    """G15 again. Traces are read by operators and often by a vendor's backend;
    the audit log is where "who" lives, under access control."""
    gateway = await build()

    await gateway.call(ALICE, "widgets.set_value", {"value": "green"})

    for span in spans.get_finished_spans():
        rendered = f"{span.name} {dict(span.attributes or {})}"
        assert "alice" not in rendered
        assert "principal" not in rendered
        assert "user" not in rendered


# -- partial installs, which are the ones that bite ------------------------


class _Blocker:
    """Makes a package unimportable, to reproduce a partial install."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def find_spec(self, name, path=None, target=None):
        if name == self.prefix or name.startswith(self.prefix + "."):
            raise ImportError(f"blocked: {name}")
        return None


def test_a_configured_endpoint_without_the_exporter_does_not_crash(monkeypatch) -> None:
    """`opentelemetry-exporter-otlp` is a separate distribution from the SDK, so
    an install can have one and not the other.

    This used to raise ModuleNotFoundError out of `configure()` and take startup
    with it. A service that refuses to start because its *telemetry* is
    misconfigured has inverted the priority: the operator wanted traces, not an
    outage.
    """
    tracing.reset()
    monkeypatch.setattr(
        "sys.meta_path", [_Blocker("opentelemetry.exporter"), *__import__("sys").meta_path]
    )

    assert tracing.configure(endpoint="http://tempo:4318/v1/traces") is False

    # And spans stay harmless afterwards rather than half-initialised.
    with tracing.span("tool x", **{"uione.tool": "x"}) as current:
        assert current is None


def test_a_provider_with_nowhere_to_send_spans_stays_off() -> None:
    """Building spans nobody exports costs the overhead and produces nothing."""
    tracing.reset()
    assert tracing.configure(endpoint="", console=False) is False
