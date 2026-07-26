"""Application wiring.

Builds the object graph once at startup and hands it to routes. Kept in one
place so the composition — which connectors, which policy, which governor — is
readable as a single unit rather than reconstructed from scattered imports.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import Header, HTTPException

from uione.agent import AgentRuntime
from uione.connectors.demo import build_all
from uione.governance import EgressPolicy, Governor
from uione.mcphub import (
    AuditLog,
    FanOutAuditSink,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    StructlogAuditSink,
    ToolPolicy,
)
from uione.modelplane import ModelPlaneClient, TaskRouter
from uione.proactive import BriefGenerator

log = structlog.get_logger(__name__)


@dataclass
class Services:
    gateway: McpGateway
    governor: Governor
    model: ModelPlaneClient
    runtime: AgentRuntime
    brief: BriefGenerator
    audit_sink: InMemoryAuditSink


_services: Services | None = None


def default_policy() -> ToolPolicy:
    """Starter grants for the demo estate.

    Reads are broad; writes are named individually. That asymmetry is the point —
    a wildcard that silently includes "send mail" is how connectors quietly widen
    everyone's reach.
    """
    return ToolPolicy(
        [
            Grant(
                role="analyst",
                tools=frozenset({"mail.*", "tasks.*", "incidents.*", "calendar.*"}),
                max_risk=RiskClass.READ,
            ),
            Grant(role="analyst", tools=frozenset({"tasks.update_issue"})),
            Grant(role="analyst", tools=frozenset({"mail.send_reply"})),
        ]
    )


async def build_services() -> Services:
    audit_sink = InMemoryAuditSink()
    governor = Governor(
        egress=EgressPolicy(internal_domains=frozenset({"corp.example"})),
    )
    gateway = McpGateway(
        policy=default_policy(),
        audit=AuditLog(FanOutAuditSink(audit_sink, StructlogAuditSink())),
        governor=governor,
    )
    for source in build_all():
        await gateway.register(source)

    model = ModelPlaneClient()
    router = TaskRouter()

    return Services(
        gateway=gateway,
        governor=governor,
        model=model,
        runtime=AgentRuntime(model=model, gateway=gateway, router=router),
        brief=BriefGenerator(model=model, gateway=gateway),
        audit_sink=audit_sink,
    )


async def startup() -> Services:
    global _services
    _services = await build_services()
    return _services


async def shutdown() -> None:
    global _services
    if _services is not None:
        await _services.model.aclose()
        _services = None


def get_services() -> Services:
    if _services is None:  # pragma: no cover — only if a route runs before startup
        raise HTTPException(status_code=503, detail="service not initialised")
    return _services


def get_principal(
    x_user_id: str = Header(default="alice"),
    x_user_roles: str = Header(default="analyst"),
    x_user_name: str = Header(default=""),
) -> Principal:
    """Resolve the caller.

    Header-based for now, and deliberately obvious about it: PR1's SSO work
    (F5.1) replaces this with OIDC. Shipping a placeholder that *looks* like real
    auth is how a placeholder reaches production.
    """
    roles = frozenset(r.strip() for r in x_user_roles.split(",") if r.strip())
    return Principal(user_id=x_user_id, roles=roles, display_name=x_user_name or x_user_id)
