"""Application wiring.

Builds the object graph once at startup and hands it to routes. Kept in one
place so the composition — which connectors, which policy, which governor — is
readable as a single unit rather than reconstructed from scattered imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

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
from uione.connectors.bi import GrafanaBI, build_grafana_source, grafana_config
from uione.connectors.calendar import CalDavBackend, CalendarAccount, build_calendar_source
from uione.connectors.claims import ClaimsBackend, build_claims_source, claims_config
from uione.connectors.demo import build_all
from uione.connectors.files import build_file_ingestion, current_identity_map
from uione.connectors.incidents import (
    ServiceNowIncidents,
    build_servicenow_source,
    servicenow_config,
)
from uione.connectors.mail import (
    ImapMailBackend,
    MailAccount,
    build_mail_source,
    register_mail_undo,
)
from uione.connectors.tasks import GiteaTasks, build_gitea_source, gitea_config
from uione.governance import EgressPolicy, Governor
from uione.identity import (
    AuthError,
    AuthMode,
    FlowSettings,
    IdentityResolver,
    OidcFlow,
    OidcSettings,
    ProxySettings,
    SessionStore,
)
from uione.knowledge import (
    DocumentIndex,
    ExtractionRules,
    IngestionRefresher,
    Ingestor,
    build_knowledge_source,
    build_mail_ingestion,
)
from uione.mcphub import (
    AuditLog,
    FanOutAuditSink,
    Grant,
    McpGateway,
    McpSupervisor,
    Principal,
    RiskClass,
    StructlogAuditSink,
    ToolPolicy,
)
from uione.modelplane import ModelPlaneClient, TaskRouter
from uione.proactive import BriefGenerator, BriefStore, Schedule, Scheduler
from uione.storage import (
    Database,
    DisclosureStore,
    DocumentStore,
    McpPinStore,
    PersistentAutonomyPolicy,
    ScheduleStore,
    SqlActionJournal,
    SqlApprovalStore,
    SqlAuditSink,
    WatermarkStore,
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
    sessions: SessionStore
    flow: OidcFlow | None
    session_ttl: timedelta
    index: DocumentIndex
    ingestor: Ingestor
    refresher: IngestionRefresher
    schedules: ScheduleStore
    disclosures: DisclosureStore
    mcp: McpSupervisor


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
                tools=frozenset(
                    {"mail.*", "tasks.*", "incidents.*", "calendar.*", "claims.*", "bi.*"}
                ),
                max_risk=RiskClass.READ,
            ),
            Grant(role="analyst", tools=frozenset({"tasks.update_issue"})),
            Grant(role="analyst", tools=frozenset({"mail.send_reply"})),
            # Named one at a time, like every other write. Both remain subject to
            # the autonomy ladder, so the grant is permission to *ask*, not
            # permission to act unattended.
            Grant(role="analyst", tools=frozenset({"incidents.update_incident"})),
            Grant(role="analyst", tools=frozenset({"claims.add_note"})),
            # Retrieval is read-only and filters by the calling principal, so a
            # broad grant here widens nothing: the index refuses what the user
            # may not read regardless of the grant.
            Grant(
                role="analyst",
                tools=frozenset({"knowledge.*"}),
                max_risk=RiskClass.READ,
            ),
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

    if settings.gitea_configured:
        # Replaces the fixture rather than joining it: two servers named "tasks"
        # would shadow each other in the catalog, and which answered would depend
        # on registration order.
        sources = [s for s in sources if s.name != "tasks"]
        sources.append(
            build_gitea_source(
                GiteaTasks(
                    gitea_config(
                        settings.gitea_url,
                        settings.gitea_token,
                        verify_tls=settings.gitea_verify_tls,
                    )
                )
            )
        )
        log.info("connectors.tasks_backend", backend="gitea", url=settings.gitea_url)
    else:
        log.info("connectors.tasks_backend", backend="fixture")

    if settings.servicenow_configured:
        sources = [s for s in sources if s.name != "incidents"]
        sources.append(
            build_servicenow_source(
                ServiceNowIncidents(
                    servicenow_config(
                        settings.servicenow_url,
                        settings.servicenow_username,
                        settings.servicenow_password,
                    ),
                    user=settings.servicenow_username,
                )
            )
        )
        log.info("connectors.incidents_backend", backend="servicenow", url=settings.servicenow_url)
    else:
        log.info("connectors.incidents_backend", backend="fixture")

    if settings.claims_configured:
        # No fixture to replace: claims are a capability this product did not
        # have until now, because no vendor in the category can be reached.
        sources.append(
            build_claims_source(
                ClaimsBackend(
                    claims_config(settings.claims_url, settings.claims_token),
                    user=settings.mail_username or "uione",
                )
            )
        )
        log.info("connectors.claims_backend", backend="cloud-api", url=settings.claims_url)

    if settings.grafana_configured:
        sources.append(
            build_grafana_source(
                GrafanaBI(grafana_config(settings.grafana_url, settings.grafana_token))
            )
        )
        log.info("connectors.bi_backend", backend="grafana", url=settings.grafana_url)

    if settings.calendar_configured:
        account = CalendarAccount(
            url=settings.calendar_url,
            username=settings.calendar_username or settings.mail_username,
            password=settings.calendar_password or settings.mail_password,
            timezone=settings.brief_timezone,
        )
        sources = [s for s in sources if s.name != "calendar"]
        sources.append(
            build_calendar_source(CalDavBackend(account), timezone=settings.brief_timezone)
        )
        log.info("connectors.calendar_backend", backend="caldav", url=settings.calendar_url)
    else:
        log.info("connectors.calendar_backend", backend="fixture")

    return sources


def build_ingestion(settings: Settings, ingestor: Ingestor) -> None:
    """Register whatever this deployment can index.

    A deployment with neither a mailbox nor a file share still gets a working
    search tool over an empty index, which answers "nothing matched" — the same
    answer it gives for a document you may not read, and deliberately so.
    """
    if settings.mail_configured:
        account = MailAccount(
            host=settings.mail_imap_host,
            port=settings.mail_imap_port,
            use_ssl=settings.mail_imap_ssl,
            username=settings.mail_username,
            password=settings.mail_password,
            mailbox=settings.mail_mailbox,
            internal_domains=settings.internal_domain_set,
        )
        ingestor.register(
            build_mail_ingestion(ImapMailBackend(account), owner_id=settings.mail_username)
        )
        log.info("ingest.source_registered", source="mail")

    if settings.files_configured:
        ingestor.register(
            build_file_ingestion(settings.files_root, identities=current_identity_map())
        )
        log.info("ingest.source_registered", source="files", root=settings.files_root)


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

    # Third-party MCP servers. They arrive under our policy rather than their
    # own: a server's risk hints cannot lower what governance requires, and its
    # tool descriptions are vetted before the model ever sees them.
    mcp = McpSupervisor.from_config(settings.mcp_servers, pins=McpPinStore(database))
    for source in await mcp.start_all():
        await gateway.register(source)

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

    # Retrieval. The index is rebuilt from stored documents rather than stored
    # itself: postings are derived from the tokeniser, and an index persisted
    # across a tokeniser change would silently disagree with its own documents.
    index = DocumentIndex()
    documents = DocumentStore(database)
    ingestor = Ingestor(index, watermarks=WatermarkStore(database), documents=documents)
    build_ingestion(settings, ingestor)
    restored = await ingestor.restore()
    await gateway.register(build_knowledge_source(index))
    log.info("retrieval.ready", documents=restored, sources=ingestor.sources)

    if settings.ingest_on_startup and ingestor.sources:
        for result in await ingestor.sync_all():
            log.info("ingest.startup", **{"result": result.summary()})

    refresher = IngestionRefresher(
        ingestor=ingestor,
        content_interval_s=settings.ingest_content_interval_s,
        acl_interval_s=settings.ingest_acl_interval_s,
        max_acl_age_s=settings.ingest_max_acl_age_s,
    )

    sessions = SessionStore(database, ttl=timedelta(minutes=settings.session_ttl_minutes))

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

    schedules = ScheduleStore(database)
    disclosures = DisclosureStore(database)
    await disclosures.load_into(contracts)

    scheduler = Scheduler(
        generator=generator,
        store=brief_store,
        principal_for=principal_for,
        max_concurrency=settings.scheduler_concurrency,
        persist=schedules.save,
    )
    scheduler.load(await schedules.load_all())

    return Services(
        gateway=gateway,
        governor=governor,
        model=model,
        runtime=AgentRuntime(model=model, gateway=gateway, router=router),
        database=database,
        brief=generator,
        audit_sink=audit_sink,
        brief_store=brief_store,
        identity=build_identity(settings, sessions),
        sessions=sessions,
        flow=build_flow(settings),
        session_ttl=timedelta(minutes=settings.session_ttl_minutes),
        a2a=a2a,
        directory=directory,
        contracts=contracts,
        scheduler=scheduler,
        index=index,
        ingestor=ingestor,
        refresher=refresher,
        schedules=schedules,
        disclosures=disclosures,
        mcp=mcp,
    )


def build_flow(settings: Settings) -> OidcFlow | None:
    """The login flow, when this deployment has one.

    Absent for proxy and dev modes: there the browser is authenticated before it
    reaches us, and offering a login button we cannot honour is worse than
    offering none.
    """
    if settings.auth_mode != "oidc" or not settings.oidc_client_id:
        return None
    return OidcFlow(
        FlowSettings(
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            scopes=settings.oidc_scopes,
            authorization_endpoint=settings.oidc_authorization_endpoint,
            token_endpoint=settings.oidc_token_endpoint,
        ),
        issuer=settings.oidc_issuer,
    )


def build_identity(settings: Settings, sessions: SessionStore | None = None) -> IdentityResolver:
    """Construct the identity resolver for this deployment.

    Raises rather than degrading if the configuration is unsafe for the
    environment, so a misconfiguration is a failed startup instead of an open
    door nobody notices.
    """
    return IdentityResolver(
        AuthMode(settings.auth_mode),
        environment=settings.environment,
        sessions=sessions,
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
    if settings.refresh_enabled:
        _services.refresher.start()
    return _services


async def shutdown() -> None:
    global _services
    if _services is not None:
        # Stop background work before closing the client it depends on, or a
        # tick in flight fails against a disposed connection pool on the way out.
        await _services.scheduler.stop()
        await _services.refresher.stop()
        await _services.mcp.aclose()
        await _services.model.aclose()
        await _services.database.dispose()
        _services = None


def get_services() -> Services:
    if _services is None:  # pragma: no cover — only if a route runs before startup
        raise HTTPException(status_code=503, detail="service not initialised")
    return _services


async def get_principal(request: Request) -> Principal:
    """Resolve the caller, or refuse the request.

    There is no default identity. The previous version of this function returned
    a user named "alice" with the analyst role when no headers were supplied,
    which would have meant every unauthenticated request arriving as a valid
    employee holding real tool grants.
    """
    try:
        return await get_services().identity.resolve(request.headers, request.cookies)
    except AuthError as exc:
        log.info("identity.refused", reason=exc.reason, path=request.url.path)
        # WWW-Authenticate so a browser or client knows what to present.
        raise HTTPException(
            status_code=401,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
