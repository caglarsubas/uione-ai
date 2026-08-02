"""Runtime configuration.

Every setting is environment-driven so the same image runs in a laptop PoC and an
air-gapped datacentre. Nothing here may default to an internet endpoint: an
on-premise product that silently phones home is a failed audit.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="UIONE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    environment: str = "dev"

    #: Bearer token Prometheus must present to scrape ``/metrics``.
    #:
    #: Empty disables the endpoint entirely, which is the default. The series
    #: describe an organisation's operational profile — approval backlog, how
    #: often writes fail to confirm, GPU burn — and serving that unauthenticated
    #: because somebody forgot to set a variable is the wrong direction to fail.
    metrics_token: str = ""

    #: OTLP/HTTP endpoint traces are exported to, e.g. http://tempo:4318/v1/traces.
    #:
    #: Empty disables tracing. There is deliberately no default: traces carry
    #: tool names, model names and the timing of an entire organisation's work,
    #: and the rule at the top of this file is that nothing may default to an
    #: internet address. Needs the `otel` extra installed; without it a
    #: configured endpoint logs a warning and tracing stays off rather than
    #: failing the service.
    otel_endpoint: str = ""
    otel_service_name: str = "uione"

    # --- Model plane (llm_inference_engine, OpenAI-compatible) ---
    model_plane_url: str = Field(
        default="http://127.0.0.1:8080/v1",
        description="Base URL of the OpenAI-compatible inference engine.",
    )
    model_plane_api_key: str = ""
    #: How many requests may be at the engine at once.
    #:
    #: Two, because measurement says more buys nothing: an 8B model served six
    #: concurrent requests in almost exactly six times the time it served one.
    #: The engine serialises internally, so extra concurrency redistributes
    #: latency rather than adding throughput — and this setting decides who
    #: waits, not how fast anything goes. Raise it for an engine that genuinely
    #: batches, such as vLLM with continuous batching.
    model_plane_concurrency: int = 2

    #: How long someone waiting on a reply queues before being told the engine
    #: is busy. Ninety seconds of spinner teaches people the product is slow;
    #: a quick refusal teaches them it is loaded, which is recoverable.
    model_plane_queue_timeout_s: float = 30.0

    model_plane_timeout_s: float = 120.0
    model_plane_connect_timeout_s: float = 10.0

    # Task-tier model assignments. Overridable per deployment; see docs/MODEL_TIERS.md.
    model_tier_triage: str = "qwen3:4b"
    model_tier_workhorse: str = "qwen3:30b-a3b"
    model_tier_reasoning: str = "qwen3:30b-a3b"
    model_tier_embedding: str = "qwen3-embedding:0.6b"

    # --- Persistence ---
    database_url: str = "sqlite+aiosqlite:///./uione.db"

    #: Run outstanding migrations at startup instead of refusing to start.
    #:
    #: Off by default, and the default is the interesting choice. Auto-upgrade
    #: is convenient for a single-node appliance and wrong for anything else:
    #: two replicas starting together both migrate, and a migration that goes
    #: badly takes production with it before anyone has read it. Refusing to
    #: start is loud, immediate, and recoverable.
    db_auto_upgrade: bool = False

    # --- Mail connector ---
    # Empty host keeps the fixture connector, so a fresh checkout runs with no
    # infrastructure. Setting a host switches to the real IMAP/SMTP backend.
    mail_imap_host: str = ""
    mail_imap_port: int = 993
    mail_imap_ssl: bool = True
    mail_username: str = ""
    mail_password: str = ""
    mail_mailbox: str = "INBOX"
    mail_smtp_host: str = ""
    mail_smtp_port: int = 587
    mail_smtp_tls: bool = True

    # --- Calendar connector ---
    # Empty URL keeps the fixture connector. A CalDAV URL switches to the real
    # one, reaching Nextcloud, Radicale, Baikal, SOGo, Zimbra and anything else
    # speaking RFC 4791.
    calendar_url: str = ""
    calendar_username: str = ""
    calendar_password: str = ""

    @property
    def calendar_configured(self) -> bool:
        return bool(self.calendar_url)

    #: Domains treated as inside the organisation. Drives external-sender
    #: detection and the egress allowlist; empty means everything is external,
    #: which is the safe direction to be wrong in.
    internal_domains: str = ""

    @property
    def internal_domain_set(self) -> frozenset[str]:
        return frozenset(d.strip().lower() for d in self.internal_domains.split(",") if d.strip())

    #: Project key prefixes that denote work items, e.g. "PAY,OPS". Known
    #: prefixes are how the work graph stays precise: a generic ABC-123 pattern
    #: also matches ISO-9001 and would manufacture links from it.
    ticket_prefixes: str = ""

    @property
    def ticket_prefix_set(self) -> frozenset[str]:
        return frozenset(p.strip().upper() for p in self.ticket_prefixes.split(",") if p.strip())

    @property
    def mail_configured(self) -> bool:
        return bool(self.mail_imap_host and self.mail_username)

    #: Language for output nobody asked for in the moment — the morning brief,
    #: the weekly review. Interactive replies match whatever the user wrote and
    #: ignore this, because the person in front of you is better evidence than
    #: a configuration file.
    locale: str = "en"

    # --- Proactive engine ---
    scheduler_enabled: bool = True
    scheduler_interval_s: float = 60.0
    brief_time: str = "07:30"
    brief_timezone: str = "UTC"
    #: Spread per user so a fleet does not all hit the model plane at once.
    brief_jitter_s: int = 900
    #: How old a stored brief may be before /brief regenerates instead.
    brief_max_age_minutes: int = 720
    #: Concurrent background generations. Deliberately small: proactive work
    #: must yield to people waiting on an interactive request.
    scheduler_concurrency: int = 2

    @property
    def brief_time_of_day(self):
        from datetime import time as _time

        hour, _, minute = self.brief_time.partition(":")
        return _time(int(hour), int(minute or 0))

    # --- Task system (Gitea / Forgejo) ---
    #: A Gitea or Forgejo instance. Self-hostable, which is why it is the first
    #: real task connector — see docs/VENDOR_ACCESS.md.
    gitea_url: str = ""
    gitea_token: str = ""
    gitea_verify_tls: bool = True

    @property
    def gitea_configured(self) -> bool:
        return bool(self.gitea_url and self.gitea_token)

    # --- Incidents (ServiceNow) ---
    #: A ServiceNow instance. Free as a Personal Developer Instance, but that
    #: needs an account the operator creates — see docs/VENDOR_ACCESS.md.
    servicenow_url: str = ""
    servicenow_username: str = ""
    servicenow_password: str = ""

    @property
    def servicenow_configured(self) -> bool:
        return bool(self.servicenow_url and self.servicenow_username)

    # --- Claims ---
    #: A claim system speaking the Guidewire Cloud API shape. No vendor in this
    #: category offers free access, so this normally points at the mock estate.
    claims_url: str = ""
    claims_token: str = ""

    @property
    def claims_configured(self) -> bool:
        return bool(self.claims_url)

    # --- BI (Grafana) ---
    #: Grafana, for dashboards and the alerts that fire off them. Self-hostable,
    #: so it is verified against a real instance. The service account only ever
    #: needs the Viewer role — see docs/BI.md.
    grafana_url: str = ""
    grafana_token: str = ""

    @property
    def grafana_configured(self) -> bool:
        return bool(self.grafana_url and self.grafana_token)

    # --- Chat (Mattermost) ---
    #: A Mattermost or compatible instance. Self-hostable, so it is verified
    #: against a real one — see docs/VENDOR_ACCESS.md.
    mattermost_url: str = ""
    mattermost_token: str = ""

    @property
    def mattermost_configured(self) -> bool:
        return bool(self.mattermost_url and self.mattermost_token)

    # --- WhatsApp Business (Meta Cloud API) ---
    #: A cloud dependency, deliberately. WhatsApp routes every message through
    #: Meta, so a deployment enabling this accepts an egress path its security
    #: team must sign off — and there is no self-hosted alternative since Meta
    #: sunset the On-Premises API. See docs/VENDOR_ACCESS.md.
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    #: Echoed back during Meta's subscription handshake.
    whatsapp_verify_token: str = ""
    #: Signs every inbound webhook. Without it the endpoint refuses to serve,
    #: because an unverified webhook is a stranger writing into the model's
    #: context window.
    whatsapp_app_secret: str = ""
    #: Whose inbox inbound messages land in. A shared business number belongs to
    #: a desk rather than a person, so this is configuration.
    whatsapp_owner: str = ""
    #: Overridable so the mock can stand in for graph.facebook.com.
    whatsapp_base_url: str = "https://graph.facebook.com"

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_phone_number_id and self.whatsapp_access_token)

    # --- MCP servers ---
    #: The third-party MCP servers this deployment connects to, as a JSON list.
    #: See docs/MCP.md. A malformed value fails startup rather than booting with
    #: silently zero connectors.
    mcp_servers: str = ""

    # --- Retrieval ---
    #: A file share to index, if this deployment has one. Empty means retrieval
    #: still works — over whatever the mailbox contributes — rather than being
    #: switched off, so search is never silently absent.
    files_root: str = ""
    #: Sub-directory of the share that assistant-written documents go into.
    #: A single directory rather than anywhere in the share: a write tool whose
    #: destination is a parameter is one prompt away from writing into somebody
    #: else's folder.
    documents_folder: str = "documents"

    #: Semantic retrieval. On when an embedding model is configured; the
    #: lexical index always runs, so switching this off narrows recall rather
    #: than removing search.
    embeddings_enabled: bool = True

    #: Whether an ingestion sweep runs at startup. Off by default: a first run
    #: over a large share should be a decision, not a surprise on boot.
    ingest_on_startup: bool = False
    #: The two refresh loops. Content is expensive and being an hour behind on a
    #: wiki page is an inconvenience; permissions are cheap and being an hour
    #: behind means someone can read a document they were removed from.
    refresh_enabled: bool = True
    ingest_content_interval_s: float = 900.0
    ingest_acl_interval_s: float = 120.0
    #: How long permissions may go unverified before the source is quarantined —
    #: its content dropped rather than served under permissions of unknown age.
    ingest_max_acl_age_s: float = 3600.0

    @property
    def files_configured(self) -> bool:
        return bool(self.files_root)

    # --- Identity ---
    # Deliberately defaults to "dev", which refuses to start outside a
    # development environment. There is no configuration that silently accepts
    # unauthenticated requests in production.
    auth_mode: str = "dev"

    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_roles_claim: str = "realm_access.roles"
    oidc_username_claim: str = "preferred_username"

    #: Login flow. Absent client id means the deployment authenticates some
    #: other way (bearer tokens, or a proxy in front) and shows no login button.
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    oidc_scopes: str = "openid profile email"
    oidc_authorization_endpoint: str = ""
    oidc_token_endpoint: str = ""
    session_ttl_minutes: int = 720

    #: Headers set by an authenticating reverse proxy, when auth_mode=proxy.
    proxy_user_header: str = "X-Forwarded-User"
    proxy_roles_header: str = "X-Forwarded-Groups"
    proxy_default_roles: str = ""

    @field_validator("auth_mode")
    @classmethod
    def _valid_auth_mode(cls, v: str) -> str:
        allowed = {"oidc", "proxy", "dev", "disabled"}
        if v not in allowed:
            raise ValueError(f"auth_mode must be one of {sorted(allowed)}")
        return v

    @property
    def proxy_default_role_set(self) -> frozenset[str]:
        return frozenset(r.strip() for r in self.proxy_default_roles.split(",") if r.strip())

    # --- Governance ---
    # Deny-by-default: an action class must be explicitly allow-listed to auto-run.
    autonomy_default_mode: str = "preview"

    @field_validator("model_plane_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("autonomy_default_mode")
    @classmethod
    def _valid_autonomy(cls, v: str) -> str:
        allowed = {"preview", "approve", "auto"}
        if v not in allowed:
            raise ValueError(f"autonomy_default_mode must be one of {sorted(allowed)}")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
