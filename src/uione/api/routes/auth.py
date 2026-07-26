"""Login, callback, logout.

The session cookie is `httpOnly`, `SameSite=Lax` and `Secure` outside
development. Lax rather than Strict because Strict would drop the cookie on the
redirect *back from the IdP*, so the user would land signed-in-but-not-really and
bounce straight into another login.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from uione.api.deps import Services, get_principal, get_services
from uione.config import get_settings
from uione.identity import COOKIE_NAME, AuthMode, ExchangeError
from uione.mcphub import Principal

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class WhoAmI(BaseModel):
    user_id: str
    display_name: str
    roles: list[str]
    auth_mode: str


def _safe_return_to(candidate: str) -> str:
    """Only ever redirect within this application.

    ``return_to`` arrives from the query string, so without this the login
    endpoint is an open redirect: an attacker sends a victim to our own trusted
    login URL and lands them on their site, already primed to enter credentials.
    """
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return "/ui/"


def _set_cookie(response: Response, session_id: str, *, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        secure=settings.environment.lower() not in {"dev", "development", "test"},
        max_age=max_age,
        path="/",
    )


@router.get("/login")
async def login(
    return_to: str = Query(default="/ui/"),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    """Begin an authorization-code flow."""
    if services.flow is None:
        raise HTTPException(status_code=404, detail="this deployment does not use OIDC login")

    url, _transaction = await services.flow.authorization_url(return_to=_safe_return_to(return_to))
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    services: Services = Depends(get_services),
) -> RedirectResponse:
    """Complete the flow and establish a session."""
    if services.flow is None:
        raise HTTPException(status_code=404, detail="this deployment does not use OIDC login")

    if error:
        log.info("identity.login_declined", error=error)
        return RedirectResponse("/ui/?login=declined", status_code=302)

    # Consuming the transaction validates state *and* makes it single-use, so a
    # replayed callback cannot mint a second session.
    transaction = services.flow.transactions.consume(state)
    if transaction is None or not code:
        log.warning("identity.callback_rejected", has_code=bool(code))
        return RedirectResponse("/ui/?login=failed", status_code=302)

    try:
        tokens = await services.flow.exchange(code=code, transaction=transaction)
    except ExchangeError:
        return RedirectResponse("/ui/?login=failed", status_code=302)

    access_token = tokens.get("access_token", "")
    try:
        # The access token is verified through the same path as a bearer token.
        # Trusting it because it came from the IdP over TLS would mean this route
        # is the one place tokens are not checked.
        principal = services.identity.verify_token(access_token)
    except Exception:  # noqa: BLE001
        log.warning("identity.callback_token_invalid")
        return RedirectResponse("/ui/?login=failed", status_code=302)

    session_id = await services.sessions.create(
        principal,
        access_token=access_token,
        refresh_token=tokens.get("refresh_token", ""),
    )

    response = RedirectResponse(transaction.return_to, status_code=302)
    _set_cookie(response, session_id, max_age=int(services.session_ttl.total_seconds()))
    return response


@router.post("/logout")
async def logout(request: Request, services: Services = Depends(get_services)) -> JSONResponse:
    """End this session.

    Revoked server-side, so signing out means the session is gone rather than
    merely forgotten by the browser. Clearing the cookie alone would leave a
    valid session behind for anyone who kept a copy of it.
    """
    session_id = request.cookies.get(COOKIE_NAME, "")
    if session_id:
        await services.sessions.revoke(session_id)

    response = JSONResponse({"status": "signed out"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/me", response_model=WhoAmI)
async def me(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> WhoAmI:
    return WhoAmI(
        user_id=principal.user_id,
        display_name=principal.display_name,
        roles=sorted(principal.roles),
        auth_mode=str(services.identity.mode),
    )


@router.get("/mode")
async def mode(services: Services = Depends(get_services)) -> dict[str, Any]:
    """How this deployment authenticates.

    Unauthenticated on purpose: the workspace has to know whether to show a
    "sign in" button before it can sign anyone in.
    """
    return {
        "mode": str(services.identity.mode),
        "login_url": "/auth/login" if services.identity.mode is AuthMode.OIDC else None,
    }
