"""A WhatsApp Business Cloud API, faked at the Graph API's own shape.

Written from Meta's Cloud API documentation. **Not verified against Meta**, and
that limit is the point of `docs/VENDOR_ACCESS.md`: getting a real number onto
the Business API takes a Meta Business account, business verification, and a
phone number that can never go back to the consumer app. This mock is what makes
the connector buildable and testable before any of that.

Two shapes are reproduced because they are the ones a connector gets wrong:

* **Sending** returns `messages[0].id` — a `wamid.` string, not the recipient's
  number and not a boolean.
* **Receiving is not an endpoint at all.** There is no way to list messages, so
  the mock offers `deliver()`, which builds the webhook envelope Meta would POST
  and signs it exactly as Meta does. A test that fabricates a simpler payload
  proves nothing about the code that has to parse the real one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel


class State:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.counter = 0


class TextBody(BaseModel):
    preview_url: bool = False
    body: str


class SendRequest(BaseModel):
    messaging_product: str
    to: str
    type: str = "text"
    recipient_type: str = "individual"
    text: TextBody | None = None


def build_whatsapp_mock(state: State | None = None) -> FastAPI:
    app = FastAPI(title="mock-whatsapp")
    app.state.data = state or State()

    @app.post("/{version}/{phone_number_id}/messages")
    async def send(version: str, phone_number_id: str, body: SendRequest, request: Request) -> dict:
        store = request.app.state.data

        if not request.headers.get("Authorization", "").startswith("Bearer "):
            # Meta answers 401 with this envelope; a connector that only checks
            # the status code misses that the detail lives under "error".
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Invalid OAuth access token", "code": 190}},
            )
        if body.messaging_product != "whatsapp":
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": "messaging_product must be whatsapp", "code": 100}},
            )

        store.counter += 1
        message_id = f"wamid.MOCK{store.counter:016d}"
        store.sent.append({"to": body.to, "body": body.text.body if body.text else ""})
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"input": body.to, "wa_id": body.to}],
            "messages": [{"id": message_id, "message_status": "accepted"}],
        }

    return app


def inbound_payload(
    *,
    sender: str,
    body: str,
    sender_name: str = "",
    message_id: str = "wamid.INBOUND0001",
    at: int | None = None,
    phone_number_id: str = "1234567890",
) -> dict:
    """The envelope Meta POSTs on an inbound message.

    Deeply nested and mostly optional, exactly as delivered — entry, changes,
    value, then messages, with the sender's display name in a *sibling* contacts
    array rather than on the message itself.
    """
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "905551112233",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {"profile": {"name": sender_name or sender}, "wa_id": sender}
                            ],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": str(at or int(time.time())),
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_payload(*, message_id: str = "wamid.MOCK1") -> dict:
    """A delivery receipt.

    Arrives on the same endpoint as a real message and carries no `messages`
    array — the case a naive parser crashes on.
    """
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": "delivered",
                                    "timestamp": str(int(time.time())),
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def sign(payload: dict, secret: str) -> tuple[bytes, str]:
    """Sign a payload the way Meta does: HMAC-SHA256 over the raw body.

    Returns the exact bytes alongside the header, because signing one
    serialisation and sending another is the mistake this helper exists to make
    impossible in tests.
    """
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"
