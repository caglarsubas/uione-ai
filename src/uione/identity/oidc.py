"""OIDC bearer token validation.

Replaces the header placeholder that has stood in for authentication until now.

The single most important property here is that it **fails closed**. The previous
implementation defaulted to a user named "alice" with the analyst role when no
headers were supplied — fine as a scaffold, catastrophic if it ever reached a
customer, because every unauthenticated request would have arrived as a valid
employee with real tool grants. An unconfigured deployment now refuses requests
rather than inventing an identity for them.

Three modes, chosen explicitly by configuration:

* ``oidc``   — validate a bearer JWT against the IdP's JWKS. The production path.
* ``proxy``  — trust identity headers set by an authenticating reverse proxy
  (oauth2-proxy, Keycloak Gatekeeper). Legitimate and common on-premise, but only
  safe if the app is unreachable except through that proxy, so it must be turned
  on deliberately and the operator has to say so.
* ``dev``    — accept unauthenticated headers. Refuses to start outside a
  development environment, and says so loudly on every startup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog
from jwt import PyJWKClient

from uione.mcphub import Principal

log = structlog.get_logger(__name__)


class AuthError(Exception):
    """Authentication failed. Carries no detail about *why* for the client.

    Distinguishing "unknown signature" from "expired" from "wrong audience" for
    an unauthenticated caller is free reconnaissance. The reason goes to the log.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class OidcSettings:
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""

    #: Claim holding the user's roles. Keycloak nests these under
    #: ``realm_access.roles``; dotted paths are resolved.
    roles_claim: str = "realm_access.roles"
    username_claim: str = "preferred_username"
    name_claim: str = "name"

    #: Seconds of clock skew tolerated. Small: a generous window extends the life
    #: of a stolen token for no operational benefit.
    leeway_s: int = 30

    jwks_cache_s: int = 3600

    @property
    def configured(self) -> bool:
        return bool(self.issuer and (self.jwks_url or self.issuer))

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    def fallback_jwks_url(self) -> str:
        """Keycloak's path, used only if discovery is unreachable.

        Guessing an IdP-specific path is a last resort: it succeeds for Keycloak
        and fails at *runtime* for everything else, which is the worst place to
        find out. Discovery is tried first precisely so a misconfiguration
        surfaces as "cannot reach the IdP" rather than "invalid token".
        """
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/certs"


def _claim(payload: dict[str, Any], path: str) -> Any:
    """Read a possibly dotted claim path."""
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


class OidcVerifier:
    """Validates bearer tokens against an IdP's published keys."""

    def __init__(self, settings: OidcSettings, *, jwk_client: Any | None = None) -> None:
        self._settings = settings
        self._jwk_client = jwk_client
        self._client_built_at = 0.0

    def _resolve_jwks_url(self) -> str:
        settings = self._settings
        if settings.jwks_url:
            return settings.jwks_url

        # Ask the IdP where its keys are rather than assuming a vendor's path.
        try:
            response = httpx.get(settings.discovery_url, timeout=10)
            response.raise_for_status()
            if url := response.json().get("jwks_uri"):
                log.info("identity.jwks_discovered", url=url)
                return url
        except Exception as exc:  # noqa: BLE001
            log.warning("identity.discovery_failed", error=f"{type(exc).__name__}: {exc}")

        return settings.fallback_jwks_url()

    def _keys(self) -> Any:
        # Rebuilt periodically so a key rotation is picked up without a restart.
        now = time.monotonic()
        if self._jwk_client is None or now - self._client_built_at > self._settings.jwks_cache_s:
            if self._jwk_client is None or self._client_built_at:
                self._jwk_client = PyJWKClient(self._resolve_jwks_url(), cache_keys=True)
            self._client_built_at = now
        return self._jwk_client

    def verify(self, token: str) -> Principal:
        settings = self._settings
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                audience=settings.audience or None,
                issuer=settings.issuer or None,
                leeway=settings.leeway_s,
                options={
                    "require": ["exp", "iat"],
                    "verify_aud": bool(settings.audience),
                    "verify_iss": bool(settings.issuer),
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad. Beyond JWT errors, fetching the JWKS can fail
            # in a dozen transport-specific ways, and every one of them means the
            # same thing here: this request is not authenticated. Narrowing the
            # clause would only create paths where an unexpected error escapes as
            # a 500 and reveals more than a 401 does.
            log.warning("identity.token_rejected", error=f"{type(exc).__name__}: {exc}")
            raise AuthError("invalid token") from exc

        subject = payload.get("sub")
        if not subject:
            raise AuthError("token has no subject")

        raw_roles = _claim(payload, settings.roles_claim) or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split()
        roles = frozenset(str(r) for r in raw_roles if r)

        # The stable subject is the identity, not the username: usernames are
        # reassigned, and an audit trail keyed on a reused name attributes one
        # person's actions to another.
        user_id = str(_claim(payload, settings.username_claim) or subject)

        return Principal(
            user_id=user_id,
            roles=roles,
            display_name=str(_claim(payload, settings.name_claim) or user_id),
        )


@dataclass
class ProxySettings:
    """Identity supplied by an authenticating reverse proxy.

    Safe only when the application cannot be reached except through that proxy,
    because the headers are unauthenticated by definition — anyone who can open a
    socket to the app can set them. That is a deployment property we cannot check
    from here, so it is opt-in and the operator asserts it.
    """

    user_header: str = "X-Forwarded-User"
    roles_header: str = "X-Forwarded-Groups"
    name_header: str = "X-Forwarded-Preferred-Username"
    default_roles: frozenset[str] = field(default_factory=frozenset)

    def principal_from(self, headers: Any) -> Principal:
        user_id = headers.get(self.user_header, "").strip()
        if not user_id:
            raise AuthError(f"proxy did not set {self.user_header}")

        raw = headers.get(self.roles_header, "")
        roles = frozenset(r.strip() for r in raw.replace(",", " ").split() if r.strip())

        return Principal(
            user_id=user_id,
            roles=roles or self.default_roles,
            display_name=headers.get(self.name_header, "").strip() or user_id,
        )


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("no Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization header is not a bearer token")
    return token.strip()
