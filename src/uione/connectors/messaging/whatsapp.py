"""WhatsApp Business, through Meta's Cloud API.

Not a curiosity. In Turkey, Brazil and India, insurers and banks receive real
customer traffic on WhatsApp — a claims desk that ignores it is ignoring its
busiest channel. That is exactly the ops-heavy, regulated buyer this product
targets, so the channel earns its place even though everything about it cuts
against the architecture.

**It cuts against the architecture, and that is worth saying plainly.** This
product's premise is on-premise and air-gapped. WhatsApp routes every message
through Meta's servers, so a deployment that enables this has a cloud dependency
and an egress path that its security team must accept deliberately. There is no
self-hosted option to fall back to: Meta sunset the On-Premises API in 2025, and
the Cloud API is now the only supported route. `docs/VENDOR_ACCESS.md` records
that as a stated decision rather than an accident.

**There is no personal-account API.** Libraries that claim otherwise reverse
WhatsApp Web, violate Meta's terms, and get numbers banned. This connector speaks
only to the Business Cloud API, and a number registered to it can no longer be
used with the consumer app — which is why a business uses a dedicated number.

**Reading and sending are asymmetric.** Sending is an HTTP call. Reading is not
possible at all: there is no endpoint that lists messages, so inbound arrives by
webhook and is read from our own inbox. Every other connector in this product
polls; this one cannot.

**The 24-hour window is a business rule with teeth.** Meta permits a free-form
reply only within 24 hours of the customer's last message. Outside it, anything
but a pre-approved template is rejected — so the connector checks before sending
rather than discovering it in an error, because "your reply was never delivered"
is not something a claims desk should learn from a customer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

#: How long after a customer's message a free-form reply is permitted.
#: Meta's rule, not ours — outside it the send is rejected.
SERVICE_WINDOW = timedelta(hours=24)

#: Longest single WhatsApp text message.
MAX_BODY_CHARS = 4096

DEFAULT_LIMIT = 20


def whatsapp_config(
    phone_number_id: str,
    access_token: str,
    *,
    base_url: str = "https://graph.facebook.com",
    api_version: str = "v21.0",
    timeout_s: float = 20.0,
) -> VendorConfig:
    """The Graph API, scoped to one business phone number."""
    return VendorConfig(
        name="whatsapp",
        base_url=f"{base_url.rstrip('/')}/{api_version}/{phone_number_id}",
        auth=Auth(scheme="bearer", secret=access_token),
        timeout_s=timeout_s,
        extra_headers={"Content-Type": "application/json"},
    )


class WhatsAppBusiness:
    """Sending. Receiving belongs to the webhook and the inbox."""

    def __init__(self, config: VendorConfig, *, inbound=None, owner: str = "", **kwargs: Any):
        self._client = VendorClient(config, **kwargs)
        self._inbound = inbound
        self._owner = owner

    async def aclose(self) -> None:
        await self._client.aclose()

    async def within_service_window(self, recipient: str, *, now: datetime | None = None) -> bool:
        """Whether a free-form reply to this number is still permitted.

        Without an inbox we cannot know, and the honest answer is no: claiming
        the window is open when it may not be produces a send that Meta rejects
        and a person who believes they replied.
        """
        if self._inbound is None:
            return False
        last = await self._inbound.last_inbound_at(self._owner, recipient)
        if last is None:
            return False
        return (now or datetime.now(UTC)) - last <= SERVICE_WINDOW

    async def send_text(self, recipient: str, body: str) -> dict:
        return await self._client.post(
            "/messages",
            json_body={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
        )


def build_whatsapp_source(
    whatsapp: WhatsAppBusiness, *, inbound=None, owner: str = "", name: str = "whatsapp"
) -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def unread_messages(args: dict) -> ToolResult:
        if inbound is None:
            return ToolResult.failure("no inbox is configured for WhatsApp")
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        rows = await inbound.unread(owner, channel="whatsapp", limit=limit)
        if not rows:
            return ToolResult.success("No unread WhatsApp messages.", {"count": 0})

        now = datetime.now(UTC)
        lines = []
        for row in rows:
            at = row.at.replace(tzinfo=UTC) if row.at.tzinfo is None else row.at
            who = row.sender_name or row.sender
            # The window is shown per message because it decides whether a reply
            # is even possible, and it expires while you read.
            hours_left = (SERVICE_WINDOW - (now - at)).total_seconds() / 3600
            window = (
                f"reply window {hours_left:.0f}h left"
                if hours_left > 0
                else "reply window CLOSED — only a template may be sent"
            )
            lines.append(f"{who} ({row.sender}) — {at:%H:%M} — {window}\n  {row.body[:400]}")

        return ToolResult.success(
            "\n".join(lines),
            {
                "count": len(rows),
                "senders": sorted({r.sender for r in rows}),
                "message_ids": [r.id for r in rows],
            },
        )

    async def send_message(args: dict) -> ToolResult:
        recipient = str(args.get("to", "")).strip()
        body = str(args.get("message", "")).strip()

        if not recipient:
            return ToolResult.failure("to is required — the customer's number in E.164 form")
        if not body:
            return ToolResult.failure("message is required")
        if len(body) > MAX_BODY_CHARS:
            return ToolResult.failure(
                f"message is {len(body)} characters; WhatsApp accepts {MAX_BODY_CHARS}"
            )

        # Checked before sending, not discovered in the error. A rejected send
        # means the customer never heard back, and nobody finds out until they
        # ask again.
        if not await whatsapp.within_service_window(recipient):
            return ToolResult.failure(
                f"the 24-hour reply window for {recipient} has closed. WhatsApp only "
                "allows a free-form reply within 24 hours of the customer's last "
                "message; after that it must be a pre-approved template, which this "
                "connector does not send."
            )

        try:
            sent = await whatsapp.send_text(recipient, body)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        message_id = ((sent.get("messages") or [{}])[0]).get("id", "")
        log.info("whatsapp.sent", to=recipient, chars=len(body))
        return ToolResult.success(
            f"Sent to {recipient}.", {"message_id": message_id, "to": recipient}
        )

    source.register(
        "unread_messages",
        unread_messages,
        description=(
            "Unread WhatsApp messages from customers, with how long the reply "
            "window has left on each."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 20."}},
        },
        risk=RiskClass.READ,
        # Written by whoever messaged the business — which is the general public,
        # so this is the least trusted content in the product.
        returns_untrusted_content=True,
    )
    source.register(
        "send_message",
        send_message,
        description=(
            "Reply to a customer on WhatsApp. Only possible within 24 hours of their last message."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "The customer's number, E.164, no plus."},
                "message": {"type": "string"},
            },
            "required": ["to", "message"],
        },
        # It reaches a member of the public on their phone, immediately, and
        # nothing takes it back. This is the highest-consequence write in the
        # product.
        risk=RiskClass.EXTERNAL_FACING,
    )
    return source
