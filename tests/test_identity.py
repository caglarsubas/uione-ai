"""Identity tests.

Tokens here are signed with a real RSA key and validated through the real
library. Mocking the verifier would test that the mock returns what it was told
to, which is exactly the part that cannot be wrong in an interesting way.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from uione.identity import (
    AuthError,
    AuthMode,
    IdentityResolver,
    InsecureConfiguration,
    OidcSettings,
    OidcVerifier,
    ProxySettings,
    bearer_token,
)

ISSUER = "https://keycloak.corp.example/realms/uione"
AUDIENCE = "uione"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwkClient:
    """Stands in for the IdP's JWKS endpoint, returning a real public key."""

    def __init__(self, key=None) -> None:
        self._key = key or KEY

    def get_signing_key_from_jwt(self, _token):
        class Signing:
            key = self._key.public_key()

        return Signing()


def make_token(
    *,
    key=KEY,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    subject: str = "e2a1-uuid",
    username: str = "alice",
    roles: list[str] | None = None,
    expires_in: int = 300,
    issued_at: int | None = None,
    include_exp: bool = True,
) -> str:
    now = int(time.time())
    payload = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": issued_at if issued_at is not None else now,
        "preferred_username": username,
        "name": "Alice Analyst",
        "realm_access": {"roles": roles if roles is not None else ["analyst", "employee"]},
    }
    if include_exp:
        payload["exp"] = now + expires_in
    return jwt.encode(payload, key, algorithm="RS256")


def verifier(**overrides) -> OidcVerifier:
    settings = OidcSettings(issuer=ISSUER, audience=AUDIENCE, **overrides)
    return OidcVerifier(settings, jwk_client=FakeJwkClient())


# -- valid tokens ----------------------------------------------------------


def test_a_valid_token_yields_a_principal() -> None:
    principal = verifier().verify(make_token())

    assert principal.user_id == "alice"
    assert principal.display_name == "Alice Analyst"
    assert principal.roles == frozenset({"analyst", "employee"})


def test_nested_role_claims_are_read() -> None:
    principal = verifier().verify(make_token(roles=["incident-responder"]))
    assert "incident-responder" in principal.roles


def test_a_custom_roles_claim_path_works() -> None:
    token = jwt.encode(
        {
            "sub": "s",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "preferred_username": "bob",
            "groups": ["finance"],
        },
        KEY,
        algorithm="RS256",
    )

    principal = verifier(roles_claim="groups").verify(token)

    assert principal.roles == frozenset({"finance"})


def test_space_separated_roles_are_split() -> None:
    token = jwt.encode(
        {
            "sub": "s",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "preferred_username": "bob",
            "scope": "analyst employee",
        },
        KEY,
        algorithm="RS256",
    )

    assert verifier(roles_claim="scope").verify(token).roles == frozenset({"analyst", "employee"})


def test_missing_roles_yield_no_roles_not_an_error() -> None:
    """Deny-by-default means a roleless token is harmless: it can call nothing."""
    principal = verifier().verify(make_token(roles=[]))
    assert principal.roles == frozenset()


def test_the_subject_is_used_when_no_username_claim_exists() -> None:
    principal = verifier(username_claim="nonexistent").verify(make_token())
    assert principal.user_id == "e2a1-uuid"


# -- rejected tokens -------------------------------------------------------


def test_a_token_signed_by_another_key_is_rejected() -> None:
    with pytest.raises(AuthError):
        verifier().verify(make_token(key=OTHER_KEY))


def test_an_expired_token_is_rejected() -> None:
    with pytest.raises(AuthError):
        verifier().verify(make_token(expires_in=-3600))


def test_a_token_without_an_expiry_is_rejected() -> None:
    """A token that never expires is a password with extra steps."""
    with pytest.raises(AuthError):
        verifier().verify(make_token(include_exp=False))


def test_a_token_for_another_audience_is_rejected() -> None:
    """Otherwise any token from the same IdP — including another app's — works."""
    with pytest.raises(AuthError):
        verifier().verify(make_token(audience="some-other-app"))


def test_a_token_from_another_issuer_is_rejected() -> None:
    with pytest.raises(AuthError):
        verifier().verify(make_token(issuer="https://evil.example/realms/x"))


def test_an_unsigned_token_is_rejected() -> None:
    """alg=none is the oldest JWT attack and must never be accepted."""
    unsigned = jwt.encode(
        {"sub": "s", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 60},
        key="",
        algorithm="none",
    )

    with pytest.raises(AuthError):
        verifier().verify(unsigned)


def test_garbage_is_rejected() -> None:
    with pytest.raises(AuthError):
        verifier().verify("not-a-token")


def test_a_token_with_no_subject_is_rejected() -> None:
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        KEY,
        algorithm="RS256",
    )

    with pytest.raises(AuthError):
        verifier().verify(token)


def test_rejection_reasons_are_not_leaked_to_the_caller() -> None:
    """Distinguishing 'expired' from 'bad signature' is free reconnaissance."""
    reasons = set()
    for token in (make_token(key=OTHER_KEY), make_token(expires_in=-1), "garbage"):
        try:
            verifier().verify(token)
        except AuthError as exc:
            reasons.add(exc.reason)

    assert reasons == {"invalid token"}


# -- bearer parsing --------------------------------------------------------


def test_bearer_token_is_extracted() -> None:
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


def test_bearer_scheme_is_case_insensitive() -> None:
    assert bearer_token("bearer abc") == "abc"


@pytest.mark.parametrize("header", [None, "", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "])
def test_non_bearer_headers_are_refused(header: str | None) -> None:
    with pytest.raises(AuthError):
        bearer_token(header)


# -- the resolver ----------------------------------------------------------


def headers(**kwargs) -> dict[str, str]:
    return kwargs


def test_disabled_mode_refuses_everything() -> None:
    """An unconfigured deployment must not invent an identity."""
    resolver = IdentityResolver(AuthMode.DISABLED)

    with pytest.raises(AuthError):
        resolver.resolve(headers(**{"X-User-Id": "alice"}))


def test_oidc_mode_requires_an_issuer() -> None:
    with pytest.raises(InsecureConfiguration):
        IdentityResolver(AuthMode.OIDC, oidc=OidcSettings())


def test_oidc_mode_resolves_a_bearer_token() -> None:
    resolver = IdentityResolver(
        AuthMode.OIDC,
        oidc=OidcSettings(issuer=ISSUER, audience=AUDIENCE),
        verifier=verifier(),
    )

    principal = resolver.resolve({"Authorization": f"Bearer {make_token()}"})

    assert principal.user_id == "alice"


def test_oidc_mode_ignores_identity_headers() -> None:
    """Otherwise the dev shortcut survives into the production path."""
    resolver = IdentityResolver(
        AuthMode.OIDC,
        oidc=OidcSettings(issuer=ISSUER, audience=AUDIENCE),
        verifier=verifier(),
    )

    with pytest.raises(AuthError):
        resolver.resolve({"X-User-Id": "attacker", "X-User-Roles": "admin"})


def test_proxy_mode_reads_forwarded_identity() -> None:
    resolver = IdentityResolver(AuthMode.PROXY)

    principal = resolver.resolve(
        {"X-Forwarded-User": "bob@corp.example", "X-Forwarded-Groups": "analyst,finance"}
    )

    assert principal.user_id == "bob@corp.example"
    assert principal.roles == frozenset({"analyst", "finance"})


def test_proxy_mode_refuses_when_the_proxy_set_nothing() -> None:
    """A request that skipped the proxy must not be treated as anonymous-but-fine."""
    with pytest.raises(AuthError):
        IdentityResolver(AuthMode.PROXY).resolve({})


def test_proxy_default_roles_apply_when_none_are_forwarded() -> None:
    resolver = IdentityResolver(
        AuthMode.PROXY, proxy=ProxySettings(default_roles=frozenset({"employee"}))
    )

    principal = resolver.resolve({"X-Forwarded-User": "bob"})

    assert principal.roles == frozenset({"employee"})


def test_dev_mode_works_in_development() -> None:
    resolver = IdentityResolver(AuthMode.DEV, environment="dev")

    principal = resolver.resolve({"X-User-Id": "alice", "X-User-Roles": "analyst,ops"})

    assert principal.user_id == "alice"
    assert principal.roles == frozenset({"analyst", "ops"})


def test_dev_mode_still_requires_a_user_id() -> None:
    with pytest.raises(AuthError):
        IdentityResolver(AuthMode.DEV, environment="dev").resolve({})


@pytest.mark.parametrize("environment", ["production", "prod", "staging"])
def test_dev_mode_refuses_to_start_outside_development(environment: str) -> None:
    """A dev shortcut that merely warns in production is one that runs there."""
    with pytest.raises(InsecureConfiguration, match="refused"):
        IdentityResolver(AuthMode.DEV, environment=environment)


def test_there_is_no_mode_that_invents_an_identity() -> None:
    """Every mode either identifies the caller or raises. None default a user."""
    for mode in AuthMode:
        if mode is AuthMode.OIDC:
            resolver = IdentityResolver(mode, oidc=OidcSettings(issuer=ISSUER), verifier=verifier())
        else:
            resolver = IdentityResolver(mode, environment="dev")

        with pytest.raises(AuthError):
            resolver.resolve({})
