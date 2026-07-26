"""Login flow and session tests.

Each of PKCE, state and nonce defends a different attack, and a flow that skips
any of them still *works* — which is exactly why they get skipped. So each is
asserted directly.
"""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from uione.config import Settings
from uione.identity import (
    FlowSettings,
    OidcFlow,
    SessionStore,
    TransactionStore,
    code_challenge_for,
)
from uione.identity.flow import TRANSACTION_TTL_S, ExchangeError
from uione.mcphub import Principal
from uione.storage import Database

ISSUER = "https://idp.corp.example/realms/uione"
ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")


def flow(**overrides) -> OidcFlow:
    settings = FlowSettings(
        client_id="uione",
        client_secret="s3cret",
        redirect_uri="https://uione.corp.example/auth/callback",
        authorization_endpoint=f"{ISSUER}/protocol/openid-connect/auth",
        token_endpoint=f"{ISSUER}/protocol/openid-connect/token",
        **overrides,
    )
    return OidcFlow(settings, issuer=ISSUER)


# -- the authorization request --------------------------------------------


async def test_authorization_url_carries_pkce_state_and_nonce() -> None:
    url, transaction = await flow().authorization_url()

    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [transaction.state]
    assert query["nonce"] == [transaction.nonce]


async def test_the_challenge_matches_the_verifier() -> None:
    """A mismatch here silently disables PKCE — the IdP would just reject later."""
    url, transaction = await flow().authorization_url()

    query = parse_qs(urlparse(url).query)
    assert query["code_challenge"] == [code_challenge_for(transaction.verifier)]


async def test_the_verifier_never_appears_in_the_url() -> None:
    """The whole point: the verifier stays on the server."""
    url, transaction = await flow().authorization_url()

    assert transaction.verifier not in url


async def test_each_attempt_gets_fresh_parameters() -> None:
    f = flow()
    _, first = await f.authorization_url()
    _, second = await f.authorization_url()

    assert first.state != second.state
    assert first.verifier != second.verifier
    assert first.nonce != second.nonce


async def test_discovery_is_used_when_endpoints_are_not_configured() -> None:
    with respx.mock:
        respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                },
            )
        )
        f = OidcFlow(FlowSettings(client_id="uione", redirect_uri="https://x/cb"), issuer=ISSUER)

        endpoints = await f.endpoints()

    assert endpoints.token == f"{ISSUER}/token"


# -- transactions ----------------------------------------------------------


def test_a_transaction_is_single_use() -> None:
    """A replayed callback must not mint a second session."""
    store = TransactionStore()
    transaction = store.start()

    assert store.consume(transaction.state) is not None
    assert store.consume(transaction.state) is None


def test_an_unknown_state_is_refused() -> None:
    """Without this the callback accepts a login started in another browser."""
    assert TransactionStore().consume("state-i-made-up") is None


def test_an_expired_transaction_is_refused() -> None:
    store = TransactionStore()
    transaction = store.start()
    transaction.created_at -= TRANSACTION_TTL_S + 1

    assert store.consume(transaction.state) is None


def test_expired_transactions_are_swept() -> None:
    store = TransactionStore()
    stale = store.start()
    stale.created_at -= TRANSACTION_TTL_S + 1

    store.start()

    assert len(store) == 1


def test_return_to_is_preserved_across_the_flow() -> None:
    store = TransactionStore()
    transaction = store.start(return_to="/ui/?panel=approvals")

    assert store.consume(transaction.state).return_to == "/ui/?panel=approvals"


# -- token exchange --------------------------------------------------------


async def test_exchange_sends_the_verifier_not_the_challenge() -> None:
    f = flow()
    _, transaction = await f.authorization_url()

    with respx.mock:
        route = respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
        )
        tokens = await f.exchange(code="the-code", transaction=transaction)

    body = route.calls.last.request.content.decode()
    assert f"code_verifier={transaction.verifier}" in body.replace("%3D", "=")
    assert tokens["access_token"] == "at"


async def test_a_failed_exchange_raises_without_leaking_the_body() -> None:
    """IdP error bodies echo request parameters; they must not reach the browser."""
    f = flow()
    _, transaction = await f.authorization_url()

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant", "code": "secret"})
        )
        with pytest.raises(ExchangeError) as caught:
            await f.exchange(code="bad", transaction=transaction)

    assert "secret" not in str(caught.value)


async def test_an_unreachable_token_endpoint_raises() -> None:
    f = flow()
    _, transaction = await f.authorization_url()

    with respx.mock:
        respx.post(f"{ISSUER}/protocol/openid-connect/token").mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(ExchangeError):
            await f.exchange(code="c", transaction=transaction)


# -- sessions --------------------------------------------------------------


@pytest.fixture
async def store(tmp_path):
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}"))
    await db.create_schema()
    yield SessionStore(db)
    await db.dispose()


async def test_a_session_round_trips(store: SessionStore) -> None:
    session_id = await store.create(ALICE, access_token="at")

    principal = await store.get(session_id)

    assert principal.user_id == "alice"
    assert principal.roles == frozenset({"analyst"})
    assert principal.display_name == "Alice"


async def test_logout_actually_revokes(store: SessionStore) -> None:
    """A self-contained cookie token would stay valid; a row can be deleted."""
    session_id = await store.create(ALICE)

    await store.revoke(session_id)

    assert await store.get(session_id) is None


async def test_an_expired_session_is_refused(tmp_path) -> None:
    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'e.db'}"))
    await db.create_schema()
    try:
        expired = SessionStore(db, ttl=timedelta(seconds=-1))
        session_id = await expired.create(ALICE)

        assert await SessionStore(db).get(session_id) is None
    finally:
        await db.dispose()


async def test_an_unknown_session_id_is_refused(store: SessionStore) -> None:
    assert await store.get("not-a-session") is None
    assert await store.get("") is None


async def test_sessions_survive_a_restart(tmp_path) -> None:
    """Otherwise every deployment silently signs everyone out."""
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    first = Database(settings)
    await first.create_schema()
    session_id = await SessionStore(first).create(ALICE)
    await first.dispose()

    second = Database(settings)
    try:
        assert (await SessionStore(second).get(session_id)).user_id == "alice"
    finally:
        await second.dispose()


async def test_revoking_all_sessions_signs_a_user_out_everywhere(store: SessionStore) -> None:
    """What an administrator needs when an account is compromised."""
    ids = [await store.create(ALICE) for _ in range(3)]
    other = await store.create(Principal(user_id="bob", roles=frozenset()))

    revoked = await store.revoke_all_for("alice")

    assert revoked == 3
    for session_id in ids:
        assert await store.get(session_id) is None
    assert await store.get(other) is not None


async def test_session_ids_are_unguessable(store: SessionStore) -> None:
    ids = {await store.create(ALICE) for _ in range(20)}

    assert len(ids) == 20
    assert all(len(session_id) >= 32 for session_id in ids)


async def test_the_token_is_not_in_the_session_id(store: SessionStore) -> None:
    """The browser holds an opaque handle, never the credential itself."""
    session_id = await store.create(ALICE, access_token="super-secret-token")

    assert "super-secret-token" not in session_id


# -- open redirect ---------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/ui/", "/ui/"),
        ("/ui/?panel=approvals", "/ui/?panel=approvals"),
        ("https://evil.example/phish", "/ui/"),
        ("//evil.example/phish", "/ui/"),
        ("http://evil.example", "/ui/"),
        ("", "/ui/"),
    ],
)
def test_return_to_cannot_leave_the_application(candidate: str, expected: str) -> None:
    """Otherwise the login endpoint is an open redirect on a trusted URL."""
    from uione.api.routes.auth import _safe_return_to

    assert _safe_return_to(candidate) == expected


def test_transaction_ttl_is_short() -> None:
    """This window is how long a stolen state value stays usable."""
    assert TRANSACTION_TTL_S <= 600


def test_a_transaction_is_not_expired_when_fresh() -> None:
    assert not TransactionStore().start().expired(now=time.monotonic())
