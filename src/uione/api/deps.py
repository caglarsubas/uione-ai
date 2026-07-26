"""Application wiring.

Builds the object graph once at startup and hands it to routes. Kept in one
place so the composition — which connectors, which policy, which governor — is
readable as a single unit rather than reconstructed from scattered imports.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from fastapi import HTTPException, Request

from uione.a2a import (
    A2ABus,
    AgentDirectory,
    ContractRegistry,
    GatewayAnswerer,
)
from uione.agent import AgentRuntime
from uione.config import Settings, get_settings
from uione.connectors.demo import build_all
from uione.connectors.mail import (
    ImapMailBackend,
    MailAccount,
    build_mail_source,
    register_mail_undo,
)
from uione.governance import EgressPolicy, Governor
from uione.identity import (
    AuthError,
    AuthMode,
    IdentityResolver,
    OidcSettings,
    ProxySettings,
)
from uione.knowledge import ExtractionRules
from uione.mcphub import (
    AuditLog,
    FanOutAuditSink,
    Grant,
    McpGateway,
    Principal,
    RiskClass,
    StructlogAuditSink,
    ToolPolicy,
)
from uione.modelplane import ModelPlaneClient, TaskRouter
from uione.proactive import BriefGenerator, BriefStore, Schedule, Scheduler
from uione.storage import (
    Database,
    PersistentAutonomyPolicy,
    SqlActionJournal,
    SqlApprovalStore,
    SqlAuditSink,
)

log = structlog.get_logger(__name__)


@dataclass
class Services:
    gateway: McpGateway
    governor: Governor
    model: ModelPlaneClient
    runtime: AgentRuntime
    brief: BriefGenerator
    audit_sink: SqlAuditSink
    database: Database
    scheduler: Scheduler
    brief_store: BriefStore
    identity: IdentityResolver
    a2a: A2ABus
    directory: AgentDirectory
    contracts: ContractRegistry


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


def build_connectors(settings: Settings) -> list:
    """Assemble the connector estate for this deployment.

    Mail is real when an IMAP host is configured and a fixture otherwise, so a
    fresh checkout runs with no infrastructure while a configured deployment
    talks to the actual mailbox. The remaining connectors are still fixtures.
    """
    sources = build_all()

    if settings.mail_configured:
        account = MailAccount(
            host=settings.mail_imap_host,
            port=settings.mail_imap_port,
            use_ssl=settings.mail_imap_ssl,
            username=settings.mail_username,
            password=settings.mail_password,
            mailbox=settings.mail_mailbox,
            smtp_host=settings.mail_smtp_host,
            smtp_port=settings.mail_smtp_port,
            smtp_use_tls=settings.mail_smtp_tls,
            internal_domains=settings.internal_domain_set,
        )
        # Replace the fixture mail source rather than adding alongside it: two
        # servers named "mail" would silently shadow each other in the catalog.
        sources = [s for s in sources if s.name != "mail"]
        sources.append(build_mail_source(ImapMailBackend(account)))
        log.info("connectors.mail_backend", backend="imap", host=settings.mail_imap_host)
    else:
        log.info("connectors.mail_backend", backend="fixture")

    return sources


async def build_services() -> Services:
    settings = get_settings()

    database = Database(settings)
    await database.create_schema()
    audit_sink = SqlAuditSink(database)

    # Autonomy is read on every mutating call, so its records are cached in
    # memory at startup and written through on each decision.
    autonomy = PersistentAutonomyPolicy(database)
    await autonomy.load()

    internal = settings.internal_domain_set
    governor = Governor(
        autonomy=autonomy,
        approvals=SqlApprovalStore(database),
        journal=SqlActionJournal(database),
        # With no configured domains every recipient is external, so outbound
        # mail is refused rather than quietly allowed anywhere.
        egress=EgressPolicy(internal_domains=internal or frozenset({"corp.example"})),
    )
    gateway = McpGateway(
        policy=default_policy(),
        audit=AuditLog(FanOutAuditSink(audit_sink, StructlogAuditSink())),
        governor=governor,
    )
    for source in build_connectors(settings):
        await gateway.register(source)
    register_mail_undo(governor.journal)

    def principal_for(user_id: str) -> Principal:
        # Roles are attached by the identity layer on a real request; a
        # background or A2A action runs with the owner's baseline role.
        return Principal(user_id=user_id, roles=frozenset({"analyst"}))

    directory = AgentDirectory()
    contracts = ContractRegistry()
    a2a = A2ABus(
        directory=directory,
        contracts=contracts,
        answerer=GatewayAnswerer(gateway, principal_for=principal_for),
        approvals=governor.approvals,
        audit=AuditLog(FanOutAuditSink(audit_sink, StructlogAuditSink())),
        principal_for=principal_for,
    )

    model = ModelPlaneClient()
    router = TaskRouter()
    generator = BriefGenerator(
        model=model,
        gateway=gateway,
        extraction_rules=ExtractionRules(
            ticket_prefixes=settings.ticket_prefix_set,
            internal_domains=internal,
        ),
    )
    brief_store = BriefStore()

    return Services(
        gateway=gateway,
        governor=governor,
        model=model,
        runtime=AgentRuntime(model=model, gateway=gateway, router=router),
        database=database,
        brief=generator,
        audit_sink=audit_sink,
        brief_store=brief_store,
        identity=build_identity(settings),
        a2a=a2a,
        directory=directory,
        contracts=contracts,
        scheduler=Scheduler(
            generator=generator,
            store=brief_store,
            principal_for=principal_for,
            max_concurrency=settings.scheduler_concurrency,
        ),
    )


def build_identity(settings: Settings) -> IdentityResolver:
    """Construct the identity resolver for this deployment.

    Raises rather than degrading if the configuration is unsafe for the
    environment, so a misconfiguration is a failed startup instead of an open
    door nobody notices.
    """
    return IdentityResolver(
        AuthMode(settings.auth_mode),
        environment=settings.environment,
        oidc=OidcSettings(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            roles_claim=settings.oidc_roles_claim,
            username_claim=settings.oidc_username_claim,
        ),
        proxy=ProxySettings(
            user_header=settings.proxy_user_header,
            roles_header=settings.proxy_roles_header,
            default_roles=settings.proxy_default_role_set,
        ),
    )


def default_schedule(settings: Settings) -> Schedule:
    return Schedule(
        at=settings.brief_time_of_day,
        timezone=settings.brief_timezone,
        jitter_s=settings.brief_jitter_s,
    )


async def startup() -> Services:
    global _services
    _services = await build_services()
    settings = get_settings()
    if settings.scheduler_enabled:
        _services.scheduler.start(interval_s=settings.scheduler_interval_s)
    return _services


async def shutdown() -> None:
    global _services
    if _services is not None:
        # Stop background work before closing the client it depends on, or a
        # tick in flight fails against a disposed connection pool on the way out.
        await _services.scheduler.stop()
        await _services.model.aclose()
        await _services.database.dispose()
        _services = None


def get_services() -> Services:
    if _services is None:  # pragma: no cover — only if a route runs before startup
        raise HTTPException(status_code=503, detail="service not initialised")
    return _services


def get_principal(request: Request) -> Principal:
    """Resolve the caller, or refuse the request.

    There is no default identity. The previous version of this function returned
    a user named "alice" with the analyst role when no headers were supplied,
    which would have meant every unauthenticated request arriving as a valid
    employee holding real tool grants.
    """
    try:
        return get_services().identity.resolve(request.headers)
    except AuthError as exc:
        log.info("identity.refused", reason=exc.reason, path=request.url.path)
        # WWW-Authenticate so a browser or client knows what to present.
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
