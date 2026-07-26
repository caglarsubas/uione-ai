"""The OIDC authorization-code flow, with PKCE.

Three parameters do the security work here and each defends a different attack:

* **PKCE** (``code_verifier`` / ``code_challenge``) — an intercepted
  authorization code is useless without the verifier, which never leaves this
  server. Mandatory in OAuth 2.1 and worth having even for a confidential client.
* **state** — binds the callback to the browser that started the flow. Without
  it, an attacker can complete a login in someone else's browser and have them
  operate as the attacker's user.
* **nonce** — binds the ID token to this particular request, so a token captured
  from another exchange cannot be replayed into it.

All three are generated per attempt, stored server-side against the transaction,
and checked on return. A flow that skips any of them still *works*, which is why
they get skipped.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
import structlog

log = structlog.get_logger(__name__)

#: How long a half-finished login stays valid. Short: this window is the time an
#: attacker has to use a stolen state value, and nobody takes ten minutes to
#: click "approve".
TRANSACTION_TTL_S = 300


def _random() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class Endpoints:
    authorization: str
    token: str
    end_session: str = ""

    @classmethod
    def from_discovery(cls, document: dict) -> Endpoints:
        return cls(
            authorization=document["authorization_endpoint"],
            token=document["token_endpoint"],
            end_session=document.get("end_session_endpoint", ""),
        )


@dataclass
class FlowSettings:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid profile email"

    #: Explicit endpoints, if discovery is unavailable — common in air-gapped
    #: estates where the IdP is reachable but its metadata is served elsewhere.
    authorization_endpoint: str = ""
    token_endpoint: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.redirect_uri)


@dataclass
class Transaction:
    """One in-flight login."""

    state: str
    verifier: str
    nonce: str
    created_at: float = field(default_factory=time.monotonic)
    return_to: str = "/ui/"

    def expired(self, *, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.created_at > TRANSACTION_TTL_S


class TransactionStore:
    """In-flight logins, held server-side and consumed exactly once.

    Single-use is the point: replaying a callback with the same state must not
    mint a second session.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Transaction] = {}

    def start(self, *, return_to: str = "/ui/") -> Transaction:
        self._sweep()
        transaction = Transaction(
            state=_random(), verifier=_random(), nonce=_random(), return_to=return_to
        )
        self._pending[transaction.state] = transaction
        return transaction

    def consume(self, state: str) -> Transaction | None:
        transaction = self._pending.pop(state, None)
        if transaction is None or transaction.expired():
            return None
        return transaction

    def _sweep(self) -> None:
        now = time.monotonic()
        for state in [s for s, t in self._pending.items() if t.expired(now=now)]:
            del self._pending[state]

    def __len__(self) -> int:
        return len(self._pending)


class OidcFlow:
    def __init__(
        self,
        settings: FlowSettings,
        *,
        issuer: str,
        transactions: TransactionStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._issuer = issuer.rstrip("/")
        self.transactions = transactions or TransactionStore()
        self._client = client
        self._endpoints: Endpoints | None = None

    async def endpoints(self) -> Endpoints:
        if self._endpoints is not None:
            return self._endpoints

        settings = self._settings
        if settings.authorization_endpoint and settings.token_endpoint:
            self._endpoints = Endpoints(
                authorization=settings.authorization_endpoint, token=settings.token_endpoint
            )
            return self._endpoints

        url = f"{self._issuer}/.well-known/openid-configuration"
        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(url)
            response.raise_for_status()
            self._endpoints = Endpoints.from_discovery(response.json())
        finally:
            if self._client is None:
                await client.aclose()
        return self._endpoints

    async def authorization_url(self, *, return_to: str = "/ui/") -> tuple[str, Transaction]:
        endpoints = await self.endpoints()
        transaction = self.transactions.start(return_to=return_to)
        settings = self._settings

        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": settings.redirect_uri,
                "scope": settings.scopes,
                "state": transaction.state,
                "nonce": transaction.nonce,
                "code_challenge": code_challenge_for(transaction.verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints.authorization}?{query}", transaction

    async def exchange(self, *, code: str, transaction: Transaction) -> dict:
        """Trade an authorization code for tokens."""
        endpoints = await self.endpoints()
        settings = self._settings

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": settings.client_id,
            "code_verifier": transaction.verifier,
        }
        if settings.client_secret:
            form["client_secret"] = settings.client_secret

        client = self._client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.post(endpoints.token, data=form)
            if response.is_error:
                # The IdP's error body can echo request parameters; log the
                # status only, and never return it to the browser.
                log.warning("identity.token_exchange_failed", status=response.status_code)
                raise ExchangeError("token exchange failed")
            return response.json()
        except httpx.HTTPError as exc:
            raise ExchangeError(f"token endpoint unreachable: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()


class ExchangeError(RuntimeError):
    pass
