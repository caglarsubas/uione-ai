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

    # --- Model plane (llm_inference_engine, OpenAI-compatible) ---
    model_plane_url: str = Field(
        default="http://127.0.0.1:8080/v1",
        description="Base URL of the OpenAI-compatible inference engine.",
    )
    model_plane_api_key: str = ""
    model_plane_timeout_s: float = 120.0
    model_plane_connect_timeout_s: float = 10.0

    # Task-tier model assignments. Overridable per deployment; see docs/MODEL_TIERS.md.
    model_tier_triage: str = "qwen3:4b"
    model_tier_workhorse: str = "qwen3:30b-a3b"
    model_tier_reasoning: str = "qwen3:30b-a3b"
    model_tier_embedding: str = "qwen3-embedding:0.6b"

    # --- Persistence ---
    database_url: str = "sqlite+aiosqlite:///./uione.db"

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

    # --- Retrieval ---
    #: A file share to index, if this deployment has one. Empty means retrieval
    #: still works — over whatever the mailbox contributes — rather than being
    #: switched off, so search is never silently absent.
    files_root: str = ""
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
