"""Inbound channels — the surface that pushes rather than answers.

Every other connector polls. It asks the mailbox what is unread, asks the
tracker what is open. WhatsApp cannot be asked: Meta's Cloud API delivers
messages to a webhook and offers **no endpoint to list them**. So this is the
first place in the product where the outside world initiates contact, and that
inverts the trust story.

**The signature check is the whole security model here.** Everywhere else,
untrusted content arrives because *we* went and fetched it from a system the
user's credentials reach. Here anyone on the internet can POST, and whatever
they send is stored, read by the assistant, and put into the model's context
window. An unverified webhook is a stranger with write access to the prompt.

So a request without a valid `X-Hub-Signature-256` is refused before its body is
looked at, and the comparison is constant-time. Meta signs with HMAC-SHA256 over
the raw body using the app secret, which means the *raw* bytes have to be
verified — re-serialising the parsed JSON produces different bytes and a
signature that never matches.

**Refusing beats accepting when the secret is missing.** A deployment that has
not configured `UIONE_WHATSAPP_APP_SECRET` gets 503 rather than an open endpoint,
because the failure mode of the other choice is silent and permanent.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from uione.api.deps import Services, get_services
from uione.config import get_settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp")
async def verify_whatsapp(request: Request) -> Response:
    """Meta's subscription handshake.

    Registering a webhook makes Meta GET this URL with a token it expects echoed
    back. The token is compared in constant time like any other secret — it is
    low value, but a timing oracle on a shared secret is a habit worth not
    forming.
    """
    settings = get_settings()
    params = request.query_params

    if params.get("hub.mode") != "subscribe":
        raise HTTPException(status_code=400, detail="unexpected hub.mode")

    expected = settings.whatsapp_verify_token
    if not expected:
        raise HTTPException(status_code=503, detail="no verify token configured")

    if not hmac.compare_digest(params.get("hub.verify_token", ""), expected):
        log.warning("webhook.verify_rejected", channel="whatsapp")
        raise HTTPException(status_code=403, detail="verify token mismatch")

    # Echoed verbatim as plain text; Meta rejects anything else, including JSON.
    return Response(content=params.get("hub.challenge", ""), media_type="text/plain")


@router.post("/whatsapp", status_code=200)
async def receive_whatsapp(
    request: Request,
    services: Services = Depends(get_services),
    x_hub_signature_256: str = Header(default=""),
) -> dict:
    """Take delivery of messages Meta pushes to us.

    Always answers 200 once the signature is valid, even if the payload is
    something this build does not understand. Meta retries a non-200 for hours
    and then gives up permanently, so a status change we cannot parse must not
    become a lost message queue.
    """
    settings = get_settings()
    secret = settings.whatsapp_app_secret
    if not secret:
        # Open rather than closed would mean anyone can write into the model's
        # context, so an unconfigured deployment refuses instead.
        raise HTTPException(status_code=503, detail="webhook is not configured")

    raw = await request.body()
    if not _signature_matches(raw, x_hub_signature_256, secret):
        log.warning("webhook.signature_rejected", channel="whatsapp", bytes=len(raw))
        raise HTTPException(status_code=403, detail="signature mismatch")

    payload = await request.json()
    stored = await _store_whatsapp(services, payload, owner=settings.whatsapp_owner)
    return {"received": stored}


def _signature_matches(raw: bytes, header: str, secret: str) -> bool:
    """HMAC-SHA256 over the *raw* body, compared in constant time.

    Raw bytes, not the parsed payload: re-serialising JSON reorders keys and
    changes whitespace, and the signature then never matches — a bug that looks
    like Meta sending bad signatures rather than like our own mistake.
    """
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


async def _store_whatsapp(services: Services, payload: dict, *, owner: str) -> int:
    """Pull messages out of Meta's envelope and record them.

    The shape is deeply nested and mostly optional, so every level is read
    defensively: a delivery receipt, a read receipt and a real message all
    arrive on this endpoint with the same outer structure.
    """
    stored = 0
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}

            # Names arrive in a sibling array keyed by phone number, not on the
            # message itself.
            names = {
                contact.get("wa_id", ""): (contact.get("profile") or {}).get("name", "")
                for contact in value.get("contacts") or []
            }

            for message in value.get("messages") or []:
                kind = message.get("type", "")
                if kind != "text":
                    # Images, documents and audio are acknowledged and not
                    # stored. Claiming to handle a claim photo by recording that
                    # one existed would be worse than saying nothing.
                    log.info("webhook.unsupported_type", channel="whatsapp", kind=kind)
                    continue

                sender = message.get("from", "")
                recorded = await services.inbound.record(
                    message_id=message.get("id", ""),
                    channel="whatsapp",
                    principal_id=owner,
                    sender=sender,
                    sender_name=names.get(sender, ""),
                    body=(message.get("text") or {}).get("body", ""),
                    at=_timestamp(message.get("timestamp")),
                )
                stored += int(recorded)

    if stored:
        log.info("webhook.received", channel="whatsapp", messages=stored)
    return stored


def _timestamp(value: str | None) -> datetime:
    """Meta sends seconds since the epoch, as a string."""
    try:
        return datetime.fromtimestamp(int(value or 0), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)
