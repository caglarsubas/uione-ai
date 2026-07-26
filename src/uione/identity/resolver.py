"""Turning a request into a :class:`Principal`, or refusing to.

One object owns the decision so there is exactly one place where an
unauthenticated request could become an identity — and one place to read when
someone asks how authentication works.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import structlog

from uione.identity.oidc import (
    AuthError,
    OidcSettings,
    OidcVerifier,
    ProxySettings,
    bearer_token,
)
from uione.mcphub import Principal

log = structlog.get_logger(__name__)


class AuthMode(StrEnum):
    OIDC = "oidc"
    PROXY = "proxy"
    DEV = "dev"
    DISABLED = "disabled"
    """Nothing configured. Every request is refused."""


class InsecureConfiguration(RuntimeError):
    """Raised when a deployment asks for something unsafe for its environment."""


class IdentityResolver:
    def __init__(
        self,
        mode: AuthMode,
        *,
        oidc: OidcSettings | None = None,
        proxy: ProxySettings | None = None,
        environment: str = "dev",
        verifier: Any | None = None,
    ) -> None:
        self.mode = mode
        self._proxy = proxy or ProxySettings()
        self._environment = environment

        if mode is AuthMode.OIDC:
            settings = oidc or OidcSettings()
            if not settings.configured:
                raise InsecureConfiguration("auth mode 'oidc' requires UIONE_OIDC_ISSUER to be set")
            self._verifier = verifier or OidcVerifier(settings)
        else:
            self._verifier = verifier

        if mode is AuthMode.DEV and environment.lower() not in {"dev", "development", "test"}:
            # Refusing to start is the point. A dev shortcut that merely warns in
            # production is a dev shortcut that runs in production.
            raise InsecureConfiguration(
                f"auth mode 'dev' accepts unauthenticated headers and is refused "
                f"in environment {environment!r}"
            )

        if mode is AuthMode.DEV:
            log.warning(
                "identity.dev_mode",
                message="ACCEPTING UNAUTHENTICATED HEADERS — development only",
            )
        elif mode is AuthMode.DISABLED:
            log.error(
                "identity.not_configured",
                message="no authentication configured; all requests will be refused",
            )
        else:
            log.info("identity.configured", mode=str(mode))

    def resolve(self, headers: Any) -> Principal:
        """Identify the caller, or raise :class:`AuthError`."""
        if self.mode is AuthMode.DISABLED:
            raise AuthError("authentication is not configured on this deployment")

        if self.mode is AuthMode.OIDC:
            return self._verifier.verify(bearer_token(headers.get("Authorization")))

        if self.mode is AuthMode.PROXY:
            return self._proxy.principal_from(headers)

        return _dev_principal(headers)


def _dev_principal(headers: Any) -> Principal:
    user_id = headers.get("X-User-Id", "").strip()
    if not user_id:
        raise AuthError("X-User-Id is required in dev mode")
    roles = frozenset(r.strip() for r in headers.get("X-User-Roles", "").split(",") if r.strip())
    return Principal(
        user_id=user_id,
        roles=roles,
        display_name=headers.get("X-User-Name", "").strip() or user_id,
    )
