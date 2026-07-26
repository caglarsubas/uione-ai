# Identity

Until this point authentication was a placeholder: `get_principal` returned a
user named **alice** with the **analyst** role whenever no headers were supplied.
Fine as a scaffold. Catastrophic if it had reached a customer, because every
unauthenticated request would have arrived as a valid employee holding real tool
grants.

The property that matters most here is that it **fails closed**. There is no
configuration in which an unidentified request becomes an identity — a test
iterates every auth mode and asserts each one raises on an empty request.

## Modes

| Mode | Use | Behaviour |
|---|---|---|
| `oidc` | Production | Validates a bearer JWT against the IdP's JWKS. Refuses to start without an issuer. |
| `proxy` | On-prem behind oauth2-proxy / Keycloak Gatekeeper | Trusts `X-Forwarded-User` etc. Opt-in only. |
| `dev` | Local development | Accepts unauthenticated headers. **Refuses to start** outside a dev environment. |
| `disabled` | Default when nothing is set | Refuses every request, loudly. |

`proxy` mode is safe only when the application is unreachable except through that
proxy — the headers are unauthenticated by definition, so anyone who can open a
socket to the app can set them. That is a deployment property we cannot verify
from inside, so the operator has to assert it by choosing the mode.

`dev` mode **raises on startup** in any non-development environment rather than
warning. A dev shortcut that merely warns in production is a dev shortcut that
runs in production.

## Token validation

Signature, `exp`, `iat`, issuer and audience are all checked. Three of those
deserve a note:

- **`exp` is required.** A token that never expires is a password with extra
  steps.
- **Audience is checked** when configured, or any token from the same IdP —
  including one minted for a different application — would be accepted.
- **`alg=none` is rejected**, since only asymmetric algorithms are permitted. It
  is the oldest JWT attack and there is a test for it.

Rejection reasons are logged but never returned. Telling an unauthenticated
caller whether a token was *expired* or *badly signed* is free reconnaissance;
they all come back as `401 not authenticated` with `WWW-Authenticate: Bearer`.

Roles come from a configurable claim path (`realm_access.roles` by default, which
is where Keycloak puts them). The **username claim is the identity, falling back
to `sub`** — but note that usernames get reassigned, and an audit trail keyed on
a reused name attributes one person's actions to another. Deployments that care
should set `UIONE_OIDC_USERNAME_CLAIM=sub`.

## Verified against a real IdP

Not mocked — a JWKS endpoint serving a real RSA public key, real signed tokens,
the app in `environment=production`, `auth_mode=oidc`:

```
valid              200
expired            401
wrong_audience     401
no token           401
dev headers        401     ← the dev shortcut does not survive into production
no_roles           200     ← authenticated…

analyst token sees : ['mail.list_unread', 'mail.send_reply', …] 6 tools
no-roles token sees: []    ← …but authorised for nothing
```

That last pair is the point. Authentication and authorisation stay separate:
a valid token with no roles is a real user who can call nothing, because the tool
policy is deny-by-default.

Separately verified: starting in `environment=production` with `auth_mode=dev`
raises `InsecureConfiguration` rather than starting.

## Configuration

```bash
UIONE_AUTH_MODE=oidc
UIONE_OIDC_ISSUER=https://keycloak.corp.example/realms/uione
UIONE_OIDC_AUDIENCE=uione
UIONE_OIDC_ROLES_CLAIM=realm_access.roles     # dotted paths resolved
UIONE_OIDC_USERNAME_CLAIM=preferred_username  # consider 'sub' for a stable id
```

JWKS defaults to the Keycloak path under the issuer; override with
`UIONE_OIDC_JWKS_URL` for other IdPs. Keys are re-fetched hourly so a rotation is
picked up without a restart.

## Not yet

The workspace still sends development identity headers and has no login flow, so
against an OIDC deployment it shows a "not signed in" notice rather than
redirecting to the IdP. The authorization-code flow is the remaining work; the
API side is complete and can be driven with a bearer token today.
