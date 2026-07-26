# The login flow

Completes **F5.1**. Identity validation landed earlier; this is how a browser
actually gets one.

## Authorization code, with PKCE

Three parameters do the security work, and each defends a different attack. A
flow that omits any of them still *works*, which is exactly why they get omitted.

| Parameter | Defends |
|---|---|
| **PKCE** (`code_verifier` / S256 challenge) | An intercepted authorization code is useless without the verifier, which never leaves the server. A test asserts the verifier does not appear in the redirect URL. |
| **state** | Binds the callback to the browser that started the flow. Without it an attacker completes a login in someone else's browser and has them operate as the attacker's user. |
| **nonce** | Binds the ID token to this request, so a token captured elsewhere cannot be replayed into it. |

Transactions are **single-use** — consuming one validates `state` *and* burns it,
so a replayed callback cannot mint a second session — and expire in five minutes,
which is the window a stolen `state` stays usable.

The token returned by the IdP is verified through **the same path as a bearer
token**. Trusting it because it arrived over TLS from the right host would make
the callback the one place tokens are not checked.

## Server-side sessions

The browser holds an opaque id in an `httpOnly` cookie; the access token stays on
the server. The second reason is the one that matters:

**Logout can actually revoke.** A self-contained token in a cookie stays valid
until it expires no matter what the user clicks, so "sign out" is a suggestion.
Deleting a row ends the session immediately — which is what an employee on a
shared machine believes is happening.

Sessions are durable, so a deployment restart does not silently sign everyone
out. `revoke_all_for(user)` exists for the compromised-account case.

`SameSite=Lax`, not `Strict`: Strict would drop the cookie on the redirect *back
from the IdP*, landing the user signed-in-but-not-really and bouncing them into
another login. `Secure` is set outside development.

`return_to` is clamped to same-origin paths. Without that, the login endpoint is
an **open redirect on a trusted URL** — an attacker sends a victim to our real
login and lands them on their site, already primed to enter credentials.

## A weakness this shipped with, found by testing it

The JWKS URL defaulted to Keycloak's path (`/protocol/openid-connect/certs`).
Against any other IdP that 404s, and the symptom is `invalid token` **at
runtime**, on every login, with nothing pointing at the cause.

The verifier now uses OIDC discovery (`.well-known/openid-configuration`) to find
`jwks_uri`, falling back to the Keycloak path only if discovery is unreachable.
A misconfiguration now surfaces as "cannot reach the IdP" rather than "your token
is invalid".

Found only because the end-to-end test used an IdP that was not Keycloak. A unit
test with a mocked key client would have passed.

## Verified end to end

Against a real IdP with authorize + token + discovery + JWKS endpoints, the app
in `environment=production`:

```
GET /auth/mode          → {"mode":"oidc","login_url":"/auth/login"}
GET /auth/me            → 401                      (before signing in)
GET /auth/login?return_to=/ui/?panel=approvals
   → IdP → callback → landed: /ui/?panel=approvals  (return_to preserved)
   IdP log: PKCE VERIFIED
   app log: jwks_discovered

GET /auth/me            → alice, roles [analyst, employee], mode oidc
GET /me/autonomy        → 6 visible tools          (session authorises the app)
cookie                  → httpOnly: True, secure: True

POST /auth/logout       → signed out
GET /auth/me            → 401
replay the old cookie   → 401                      (revoked, not just forgotten)
```

## The workspace

Asks `/auth/mode` before anything else, so a signed-out user sees a **Sign in**
button rather than a wall of failed panels, and is returned to whatever they were
looking at. It sends dev identity headers only when the server reports `dev` mode
— which it refuses to be outside a development environment.

## Configuration

```bash
UIONE_AUTH_MODE=oidc
UIONE_OIDC_ISSUER=https://keycloak.corp.example/realms/uione
UIONE_OIDC_AUDIENCE=uione
UIONE_OIDC_CLIENT_ID=uione
UIONE_OIDC_CLIENT_SECRET=...          # from the secrets manager
UIONE_OIDC_REDIRECT_URI=https://uione.corp.example/auth/callback
UIONE_SESSION_TTL_MINUTES=720
```
