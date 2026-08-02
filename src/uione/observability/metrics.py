"""Prometheus metrics — part of F11.1, and the gap named in OPERATIONS.md.

Structured logs say what happened once. An operator asking "is the assistant
getting slower", "how many writes did we fail to confirm this week", or "which
department is burning the GPU" needs those same events counted over time, and
until now there was nothing for Prometheus to scrape.

**Almost everything here is derived from the audit stream.** Every tool call
already passes through the audit tap, carrying server, tool, risk, outcome,
duration and — since F2.6 — the read-after-write verdict. Counting those as they
go past means the metrics cannot drift from the audit log: they are the same
events, added up. A second instrumentation path would eventually disagree with
the first, and then nobody would trust either.

**No per-user labels, ever.** Not a cardinality concern, though it is also that:
a metrics endpoint labelled by user id is a surveillance surface, and the privacy
stance (G15) promises admins aggregate-only analytics. "Which of my reports used
the assistant least this week" must not be answerable from a Prometheus query.
Per-user attribution lives in the audit log, which is access-controlled and
exists for auditors rather than managers.

**Hand-rolled, and no new dependency.** The exposition format is a few lines of
text, and the alternative — `prometheus_client` — is another package in an
air-gapped bundle for a feature that needs counters and sums. The same reasoning
that keeps the web client build-free.

**Off unless configured.** No token, no endpoint: the metrics say how many
approvals a deployment holds and how often writes fail to confirm, which is not
information to serve to whoever asks. Fails closed like everything else.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    # Type-only, so this package has no *runtime* dependency on mcphub. The
    # gateway imports `observability.tracing`, and a runtime import back the
    # other way would make that a cycle resolved by import order — which works
    # until somebody imports the two modules in the opposite sequence.
    from uione.mcphub.audit import AuditRecord

#: Label sets are tuples of (name, value) pairs, sorted, so the same labels in a
#: different order are the same series.
Labels = tuple[tuple[str, str], ...]


def _labels(**kwargs: str) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in kwargs.items()))


def _escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _render_labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in labels)
    return "{" + inner + "}"


@dataclass
class Counter:
    """Monotonic count per label set."""

    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self.values[_labels(**labels)] += amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


@dataclass
class Summary:
    """Count and sum per label set.

    A summary rather than a histogram, deliberately. A histogram needs buckets
    chosen up front, and buckets chosen badly are worse than none — they answer
    the wrong question confidently and cannot be re-cut after the fact. Count and
    sum give an operator the average and the rate, which is what "is it getting
    slower" actually needs. Percentiles can come when somebody has a latency SLO
    to hold the buckets to.
    """

    name: str
    help: str
    counts: dict[Labels, int] = field(default_factory=lambda: defaultdict(int))
    sums: dict[Labels, float] = field(default_factory=lambda: defaultdict(float))

    def observe(self, value: float, **labels: str) -> None:
        key = _labels(**labels)
        self.counts[key] += 1
        self.sums[key] += value

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} summary"]
        for labels, count in sorted(self.counts.items()):
            rendered = _render_labels(labels)
            lines.append(f"{self.name}_count{rendered} {count}")
            lines.append(f"{self.name}_sum{rendered} {self.sums[labels]:g}")
        return lines


@dataclass
class Gauge:
    """A value read at scrape time rather than accumulated."""

    name: str
    help: str
    values: dict[Labels, float] = field(default_factory=dict)

    def set(self, value: float, **labels: str) -> None:
        self.values[_labels(**labels)] = value

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {value:g}")
        return lines


class MetricsRegistry:
    """Every series this product publishes.

    Held in one object rather than module globals so tests get a clean one and
    two apps in a process do not share counters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        self.tool_calls = Counter(
            "uione_tool_calls_total",
            "Tool calls through the gateway, by outcome.",
        )
        self.tool_duration = Summary(
            "uione_tool_call_duration_seconds",
            "Time connectors took to answer. Excludes read-after-write.",
        )
        self.verified_actions = Counter(
            "uione_verified_actions_total",
            "Mutating actions read back and confirmed. The north-star numerator.",
        )
        self.unconfirmed_actions = Counter(
            "uione_unconfirmed_actions_total",
            "Mutating actions whose read-back contradicted them. Alert on any increase.",
        )
        self.mutations = Counter(
            "uione_mutating_actions_total",
            "Mutating actions that executed, verified or not. The north-star denominator.",
        )
        self.model_tokens = Counter(
            "uione_model_tokens_total",
            "Tokens by model and kind. Aggregate — never labelled by user (G15).",
        )
        self.model_requests = Counter(
            "uione_model_requests_total",
            "Completions requested, by model.",
        )
        self.connector_up = Gauge(
            "uione_connector_up",
            "1 when a connector's circuit is closed, 0 when the gateway has given up on it.",
        )
        self.model_plane_in_flight = Gauge(
            "uione_model_plane_in_flight",
            "Requests at the engine now.",
        )
        self.model_plane_queued = Gauge(
            "uione_model_plane_queued",
            "Requests waiting for a slot. The number worth alerting on.",
        )
        self.approvals_pending = Gauge(
            "uione_approvals_pending",
            "Actions waiting for a human decision.",
        )

    # -- recording ---------------------------------------------------------

    def observe_audit(self, record: AuditRecord) -> None:
        """Count one tool call, from the record the audit tap already produced."""
        with self._lock:
            self.tool_calls.inc(
                server=record.server,
                tool=record.tool,
                risk=str(record.risk),
                outcome=str(record.outcome),
            )
            self.tool_duration.observe(record.duration_ms / 1000.0, server=record.server)

            if record.mutating_and_succeeded or record.verification is not None:
                # A contradicted write executed, so it belongs in the denominator
                # even though its outcome is UNCONFIRMED rather than ALLOWED.
                self.mutations.inc(server=record.server, tool=record.tool)

            if record.verified:
                self.verified_actions.inc(server=record.server, tool=record.tool)
            elif record.verification == "contradicted":
                self.unconfirmed_actions.inc(server=record.server, tool=record.tool)

    def observe_usage(self, by_model: dict, calls: int) -> None:
        """Set token totals from the model plane's recorder.

        Assignment rather than increment: the recorder already accumulates, and
        adding its running totals on every scrape would multiply them.
        """
        with self._lock:
            for model, usage in by_model.items():
                self.model_tokens.values[_labels(model=model, kind="prompt")] = usage.prompt_tokens
                self.model_tokens.values[_labels(model=model, kind="completion")] = (
                    usage.completion_tokens
                )
            self.model_requests.values[_labels()] = calls

    # -- exposition --------------------------------------------------------

    def render(self) -> str:
        with self._lock:
            blocks = [
                self.tool_calls,
                self.tool_duration,
                self.verified_actions,
                self.unconfirmed_actions,
                self.mutations,
                self.model_requests,
                self.model_tokens,
                self.connector_up,
                self.model_plane_in_flight,
                self.model_plane_queued,
                self.approvals_pending,
            ]
            lines: list[str] = []
            for block in blocks:
                lines.extend(block.render())
        return "\n".join(lines) + "\n"


class MetricsAuditSink:
    """An :class:`uione.mcphub.audit.AuditSink` that counts instead of storing.

    Registered alongside the real sinks in the fan-out, so metrics and the audit
    log are fed by one event rather than two code paths that can disagree.
    """

    def __init__(self, registry: MetricsRegistry) -> None:
        self._registry = registry

    async def write(self, record: AuditRecord) -> None:
        self._registry.observe_audit(record)
