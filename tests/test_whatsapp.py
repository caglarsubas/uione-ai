"""WhatsApp Business, and the inbound webhook it needs.

Two clusters matter more than the rest.

The **signature check** is the whole security model. Everywhere else in this
product untrusted content arrives because we went and fetched it with the user's
credentials. Here anyone on the internet can POST, and what they send is stored
and read into the model's context window — so an unverified webhook is a stranger
with write access to the prompt.

The **24-hour window** is a business rule with teeth. Meta rejects a free-form
reply outside it, and a rejected reply means the customer never heard back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from uione.config import Settings
from uione.connectors.messaging import (
    MAX_BODY_CHARS,
    WhatsAppBusiness,
    build_whatsapp_source,
    whatsapp_config,
)
from uione.mcphub import RiskClass
from uione.storage import Database, InboundStore
from uione.vendormocks.whatsapp import (
    State,
    build_whatsapp_mock,
    inbound_payload,
    sign,
    status_payload,
)

OWNER = "claims-desk"
CUSTOMER = "905551112233"
SECRET = "app-secret"


@pytest.fixture
async def inbound(tmp_path):
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"))
    await database.create_schema()
    yield InboundStore(database)
    await database.dispose()


def business(inbound=None, *, state: State | None = None) -> WhatsAppBusiness:
    app = build_whatsapp_mock(state or State())
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://graph.mock/v21.0/1234567890"
    )
    return WhatsAppBusiness(
        whatsapp_config("1234567890", "token", base_url="http://graph.mock"),
        inbound=inbound,
        owner=OWNER,
        client=client,
    )


# -- the webhook signature ---------------------------------------------------


def test_a_valid_signature_is_accepted() -> None:
    from uione.api.routes.webhooks import _signature_matches

    raw, header = sign(inbound_payload(sender=CUSTOMER, body="hi"), SECRET)

    assert _signature_matches(raw, header, SECRET)


def test_a_forged_signature_is_refused() -> None:
    """Without this, anyone on the internet can write into the model's context."""
    from uione.api.routes.webhooks import _signature_matches

    raw, _ = sign(inbound_payload(sender=CUSTOMER, body="hi"), SECRET)

    assert not _signature_matches(raw, "sha256=" + "0" * 64, SECRET)


def test_a_signature_from_a_different_secret_is_refused() -> None:
    from uione.api.routes.webhooks import _signature_matches

    raw, header = sign(inbound_payload(sender=CUSTOMER, body="hi"), "someone-elses-secret")

    assert not _signature_matches(raw, header, SECRET)


def test_a_missing_signature_is_refused() -> None:
    from uione.api.routes.webhooks import _signature_matches

    raw, _ = sign(inbound_payload(sender=CUSTOMER, body="hi"), SECRET)

    assert not _signature_matches(raw, "", SECRET)
    assert not _signature_matches(raw, "deadbeef", SECRET)


def test_the_signature_covers_the_raw_body_not_the_reparsed_one() -> None:
    """Re-serialising the parsed JSON reorders keys and changes whitespace, so
    the signature never matches — a bug that looks like Meta sending bad
    signatures rather than like our own mistake.
    """
    from uione.api.routes.webhooks import _signature_matches

    payload = inbound_payload(sender=CUSTOMER, body="hi")
    raw, header = sign(payload, SECRET)
    reserialised = json.dumps(payload, indent=2).encode()

    assert _signature_matches(raw, header, SECRET)
    assert not _signature_matches(reserialised, header, SECRET)


# -- what the webhook stores -------------------------------------------------


async def test_an_inbound_message_is_recorded(inbound: InboundStore) -> None:
    payload = inbound_payload(
        sender=CUSTOMER, body="my claim CLM-004401 is stuck", sender_name="Ayşe"
    )

    await _deliver(inbound, payload)
    rows = await inbound.unread(OWNER, channel="whatsapp")

    assert [r.body for r in rows] == ["my claim CLM-004401 is stuck"]
    assert rows[0].sender_name == "Ayşe"


async def test_a_redelivered_webhook_is_not_a_second_message(inbound: InboundStore) -> None:
    """Meta redelivers what it believes failed, including deliveries that
    succeeded and lost the acknowledgement."""
    payload = inbound_payload(sender=CUSTOMER, body="hello", message_id="wamid.SAME")

    await _deliver(inbound, payload)
    await _deliver(inbound, payload)

    assert len(await inbound.unread(OWNER, channel="whatsapp")) == 1


async def test_a_delivery_receipt_does_not_crash_or_store(inbound: InboundStore) -> None:
    """Status callbacks arrive on the same endpoint with no `messages` array —
    the shape a naive parser breaks on."""
    await _deliver(inbound, status_payload())

    assert await inbound.unread(OWNER, channel="whatsapp") == []


async def test_an_unsupported_type_is_acknowledged_and_not_stored(inbound: InboundStore) -> None:
    """Recording that an image existed, without its content, would be worse than
    saying nothing — a claims desk would think it had the photo."""
    payload = inbound_payload(sender=CUSTOMER, body="")
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": CUSTOMER,
        "id": "wamid.IMG",
        "timestamp": "1700000000",
        "type": "image",
        "image": {"id": "media-1"},
    }

    await _deliver(inbound, payload)

    assert await inbound.unread(OWNER, channel="whatsapp") == []


async def _deliver(inbound: InboundStore, payload: dict) -> None:
    from uione.api.routes.webhooks import _store_whatsapp

    class _Services:
        pass

    services = _Services()
    services.inbound = inbound
    await _store_whatsapp(services, payload, owner=OWNER)


# -- the 24-hour window ------------------------------------------------------


async def test_a_reply_inside_the_window_is_sent(inbound: InboundStore) -> None:
    await inbound.record(
        message_id="m1",
        channel="whatsapp",
        principal_id=OWNER,
        sender=CUSTOMER,
        body="is my claim moving?",
    )
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("send_message", {"to": CUSTOMER, "message": "Looking now."})

    assert result.ok
    assert result.structured["message_id"].startswith("wamid.")


async def test_a_reply_outside_the_window_is_refused_before_sending(
    inbound: InboundStore,
) -> None:
    """Checked rather than discovered in an error: a rejected send means the
    customer never heard back, and nobody finds out until they ask again."""
    await inbound.record(
        message_id="m1",
        channel="whatsapp",
        principal_id=OWNER,
        sender=CUSTOMER,
        body="hello",
        at=datetime.now(UTC) - timedelta(hours=30),
    )
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("send_message", {"to": CUSTOMER, "message": "Sorry for the delay."})

    assert not result.ok
    assert "24-hour" in (result.error or "")
    assert "template" in (result.error or "")


async def test_a_stranger_cannot_be_messaged(inbound: InboundStore) -> None:
    """No inbound message means no window, and no window means no send. An
    assistant that can open a WhatsApp conversation with an arbitrary number is
    a cold-messaging tool."""
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("send_message", {"to": "905559998877", "message": "Hello!"})

    assert not result.ok


async def test_the_window_is_shown_on_each_unread_message(inbound: InboundStore) -> None:
    """It decides whether a reply is possible at all, and it expires while you
    read."""
    await inbound.record(
        message_id="m1",
        channel="whatsapp",
        principal_id=OWNER,
        sender=CUSTOMER,
        body="still waiting",
        at=datetime.now(UTC) - timedelta(hours=2),
    )
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("unread_messages", {})

    assert "reply window" in result.content
    assert "22h left" in result.content


async def test_a_closed_window_says_so_in_the_listing(inbound: InboundStore) -> None:
    await inbound.record(
        message_id="m1",
        channel="whatsapp",
        principal_id=OWNER,
        sender=CUSTOMER,
        body="anyone?",
        at=datetime.now(UTC) - timedelta(hours=40),
    )
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("unread_messages", {})

    assert "CLOSED" in result.content


# -- sending -----------------------------------------------------------------


async def test_an_over_long_message_is_refused(inbound: InboundStore) -> None:
    await inbound.record(
        message_id="m1", channel="whatsapp", principal_id=OWNER, sender=CUSTOMER, body="hi"
    )
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call(
        "send_message", {"to": CUSTOMER, "message": "x" * (MAX_BODY_CHARS + 1)}
    )

    assert not result.ok
    assert str(MAX_BODY_CHARS) in (result.error or "")


async def test_an_empty_message_is_refused(inbound: InboundStore) -> None:
    source = build_whatsapp_source(business(inbound), inbound=inbound, owner=OWNER)

    result = await source.call("send_message", {"to": CUSTOMER, "message": "   "})

    assert not result.ok


async def test_the_send_reaches_the_api_in_metas_shape(inbound: InboundStore) -> None:
    """`messaging_product` is required and the mock rejects its absence, which is
    what Meta does."""
    await inbound.record(
        message_id="m1", channel="whatsapp", principal_id=OWNER, sender=CUSTOMER, body="hi"
    )
    state = State()
    source = build_whatsapp_source(business(inbound, state=state), inbound=inbound, owner=OWNER)

    await source.call("send_message", {"to": CUSTOMER, "message": "On it."})

    assert state.sent == [{"to": CUSTOMER, "body": "On it."}]


# -- classification ----------------------------------------------------------


async def test_sending_is_external_facing(inbound: InboundStore) -> None:
    """It reaches a member of the public on their phone, immediately, and
    nothing takes it back."""
    specs = {
        s.tool: s
        for s in await build_whatsapp_source(
            business(inbound), inbound=inbound, owner=OWNER
        ).list_tools()
    }

    assert specs["send_message"].risk is RiskClass.EXTERNAL_FACING


async def test_inbound_content_is_untrusted(inbound: InboundStore) -> None:
    """Written by whoever messaged the business — the general public, which
    makes this the least trusted content in the product."""
    specs = {
        s.tool: s
        for s in await build_whatsapp_source(
            business(inbound), inbound=inbound, owner=OWNER
        ).list_tools()
    }

    assert specs["unread_messages"].returns_untrusted_content
