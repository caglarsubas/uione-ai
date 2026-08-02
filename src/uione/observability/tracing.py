"""Distributed tracing — the rest of F11.1.

Metrics answer "how often" and "how slow on average". They cannot answer the
question an operator actually gets asked, which is **"why was *this* brief
slow?"** — a single request that fanned out to seven connectors and three model
calls, where the answer is one span at the bottom of a tree.

`pyproject.toml` has declared an ``otel`` extra since before this module existed,
and nothing imported it. This is that extra becoming real.

**Optional, and genuinely optional.** OpenTelemetry is not a base dependency: an
air-gapped bundle should not carry a tracing SDK for a deployment that exports
nowhere. Every function here works with the packages absent — `span()` becomes a
context manager that does nothing, at the cost of one attribute lookup. That is
not a degraded mode to apologise for; it is the default, and the one CI exercises
in the base install.

**Off unless an endpoint is configured**, and the endpoint may not default to
anything. The rule in `config.py` is that nothing may default to an internet
address, and a tracing exporter is the most obvious way to breach it — traces
carry tool names, model names, and timing for an entire organisation.

**No user identifiers on spans.** Same reasoning as the metrics endpoint: traces
land in a system operators and often vendors can read, and G15 promises employees
their assistant is not a surveillance channel. Spans carry *what the system did*
— tool, server, risk, outcome, model, token counts — never who asked. The audit
log answers "who", under access control, for auditors.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import structlog

log = structlog.get_logger(__name__)

try:  # pragma: no cover — exercised by whichever install this is
    from opentelemetry import trace as _otel_trace

    AVAILABLE = True
except ImportError:  # pragma: no cover
    _otel_trace = None  # type: ignore[assignment]
    AVAILABLE = False


_tracer: Any | None = None


def _primitive(value: Any) -> Any:
    """OTel rejects arbitrary objects, and a span that raises while being
    annotated would turn observability into an outage."""
    return value if isinstance(value, bool | int | float) else str(value)


def configure(
    *,
    endpoint: str = "",
    service_name: str = "uione",
    console: bool = False,
) -> bool:
    """Install the SDK and start exporting. Returns whether tracing is on.

    Idempotent, and safe to call when the packages are absent — it reports
    ``False`` rather than raising, because a missing optional dependency is a
    deployment choice and not an error to crash a service over.
    """
    global _tracer

    if not AVAILABLE:
        if endpoint:
            # Worth saying out loud: somebody set the variable expecting traces.
            log.warning(
                "tracing.unavailable",
                endpoint=endpoint,
                reason="install the 'otel' extra to export traces",
            )
        return False

    if not endpoint and not console:
        return False

    if _tracer is not None:
        return True

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        # `opentelemetry-api` without `opentelemetry-sdk`. Common, because other
        # libraries depend on the api package alone.
        log.warning("tracing.sdk_missing", reason="install the 'otel' extra")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporters = 0

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            # The exporter is a separate distribution from the SDK, so this is
            # reachable with a partial install. It must not take the service
            # down: the operator wanted traces, not an outage, and a process
            # that refuses to start because its telemetry is misconfigured has
            # inverted the priority.
            log.error(
                "tracing.exporter_missing",
                endpoint=endpoint,
                reason="install opentelemetry-exporter-otlp; running without traces",
            )
        else:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            exporters += 1
    if console:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        exporters += 1

    if not exporters:
        # A provider with nowhere to send spans costs the overhead of building
        # them and produces nothing anybody can read.
        return False

    _otel_trace.set_tracer_provider(provider)
    _tracer = _otel_trace.get_tracer("uione")
    log.info("tracing.configured", endpoint=endpoint or "console", service=service_name)
    return True


def reset() -> None:
    """Forget the configured tracer. For tests, which need a clean provider."""
    global _tracer
    _tracer = None


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span, or nothing at all.

    Attribute values are coerced to primitives because OTel rejects arbitrary
    objects, and a span that throws while being annotated would turn observability
    into an outage. ``None`` values are dropped rather than stringified to
    ``"None"``, which is noise in every trace viewer.
    """
    if _tracer is None:
        yield None
        return

    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is None:
                continue
            current.set_attribute(key, _primitive(value))
        yield current


def annotate(current: Any, **attributes: Any) -> None:
    """Add attributes to a span once the outcome is known.

    Most of what is worth recording — the outcome, the verdict, the token count —
    only exists after the work is done, and a span opened with everything it will
    ever say would have to be opened too late to time anything.
    """
    if current is None:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        with contextlib.suppress(Exception):
            current.set_attribute(key, _primitive(value))


def instrument_app(app: Any) -> bool:
    """Auto-instrument FastAPI, so a trace starts at the HTTP request."""
    if _tracer is None:
        return False
    try:  # pragma: no cover — depends on the optional extra
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        log.warning("tracing.fastapi_instrumentation_unavailable")
        return False

    FastAPIInstrumentor.instrument_app(app)
    return True
