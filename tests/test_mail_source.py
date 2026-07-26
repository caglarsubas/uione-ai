from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uione.connectors.mail import (
    InMemoryMailBackend,
    MailMessage,
    build_mail_source,
    register_mail_undo,
)
from uione.connectors.mail.source import MAX_LIMIT
from uione.governance import Governor
from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))


def message(uid: str, subject: str, *, external: bool = False, unread: bool = True) -> MailMessage:
    return MailMessage(
        uid=uid,
        subject=subject,
        from_address="x@outside.example" if external else "cfo@corp.example",
        body=f"Body of {subject}",
        date=datetime(2026, 7, 27, 8, int(uid) % 60, tzinfo=UTC),
        unread=unread,
        external=external,
    )


@pytest.fixture
def backend() -> InMemoryMailBackend:
    return InMemoryMailBackend(
        messages=[
            message("1", "Budget review moved"),
            message("2", "Invoice discrepancy", external=True),
            message("3", "Already handled", unread=False),
        ]
    )


async def build_gateway(backend: InMemoryMailBackend, **kwargs) -> tuple[McpGateway, Governor]:
    governor = Governor()
    gateway = McpGateway(
        policy=kwargs.pop("policy", None)
        or ToolPolicy(
            [
                Grant(role="analyst", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ),
                Grant(role="analyst", tools=frozenset({"mail.mark_read", "mail.send_reply"})),
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
        governor=governor,
    )
    await gateway.register(build_mail_source(backend))
    return gateway, governor


# -- reads -----------------------------------------------------------------


async def test_unread_listing_excludes_read_messages(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.list_unread")

    assert call.ok
    assert "Budget review moved" in call.result.content
    assert "Already handled" not in call.result.content


async def test_unread_listing_reports_external_senders(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.list_unread")

    assert call.result.structured["external_senders"] == 1
    assert "EXTERNAL SENDER" in call.result.content


async def test_search_finds_by_subject(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.search", {"query": "invoice"})

    assert "Invoice discrepancy" in call.result.content


async def test_search_without_a_query_is_refused(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.search", {"query": "  "})

    assert not call.ok
    assert "required" in (call.result.error or "")


async def test_empty_result_is_success_not_failure(backend: InMemoryMailBackend) -> None:
    """'Nothing matched' is an answer; treating it as an error makes agents retry."""
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.search", {"query": "nonexistent"})

    assert call.ok
    assert call.result.structured["count"] == 0


async def test_oversized_limit_is_clamped(backend: InMemoryMailBackend) -> None:
    """A model asking for 10,000 messages is optimistic, not malicious."""
    backend.messages = [message(str(i), f"Subject {i}") for i in range(1, 200)]
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.list_unread", {"limit": 10_000})

    assert call.result.structured["count"] == MAX_LIMIT


async def test_nonsense_limit_falls_back_to_the_default(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.list_unread", {"limit": "lots"})

    assert call.ok


async def test_get_message_returns_a_fuller_body(backend: InMemoryMailBackend) -> None:
    backend.messages = [
        MailMessage(uid="1", subject="Long", body="x" * 3000, date=None, external=False)
    ]
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.get_message", {"uid": "1"})

    assert "truncated" not in call.result.content


async def test_missing_message_is_a_clear_failure(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.get_message", {"uid": "999"})

    assert not call.ok
    assert "999" in (call.result.error or "")


async def test_server_outage_degrades_rather_than_raising(backend: InMemoryMailBackend) -> None:
    backend.fail_with = "IMAP server unreachable"
    gateway, _ = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.list_unread")

    assert not call.ok
    assert "unreachable" in (call.result.error or "")


# -- governance ------------------------------------------------------------


async def test_reading_mail_taints_via_the_declared_trust(backend: InMemoryMailBackend) -> None:
    """Reads must declare untrusted content, or containment never engages."""
    gateway, _ = await build_gateway(backend)

    assert gateway.spec("mail.list_unread").returns_untrusted_content
    assert gateway.spec("mail.search").returns_untrusted_content
    assert gateway.spec("mail.get_message").returns_untrusted_content


async def test_sending_is_classified_external_facing(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)
    assert gateway.spec("mail.send_reply").risk is RiskClass.EXTERNAL_FACING


async def test_marking_read_is_a_reversible_write(backend: InMemoryMailBackend) -> None:
    gateway, _ = await build_gateway(backend)
    assert gateway.spec("mail.mark_read").risk is RiskClass.REVERSIBLE_WRITE


async def test_sending_is_held_for_approval(backend: InMemoryMailBackend) -> None:
    gateway, governor = await build_gateway(backend)

    call = await gateway.call(ALICE, "mail.send_reply", {"to": ["cfo@corp.example"], "body": "ok"})

    assert call.held
    assert backend.sent == []
    assert await governor.approvals.pending_for(ALICE)


async def test_approved_send_reaches_the_backend(backend: InMemoryMailBackend) -> None:
    gateway, governor = await build_gateway(backend)
    held = await gateway.call(ALICE, "mail.send_reply", {"to": ["cfo@corp.example"], "body": "ok"})

    context = await governor.approve(held.pending_action_id)
    done = await gateway.call(
        ALICE,
        "mail.send_reply",
        {"to": ["cfo@corp.example"], "body": "ok"},
        context=context,
    )

    assert done.ok
    assert backend.sent[0]["to"] == ["cfo@corp.example"]


async def test_read_only_grant_cannot_send(backend: InMemoryMailBackend) -> None:
    policy = ToolPolicy(
        [Grant(role="analyst", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ)]
    )
    gateway, _ = await build_gateway(backend, policy=policy)

    call = await gateway.call(ALICE, "mail.send_reply", {"to": ["x@y.example"], "body": "b"})

    assert not call.ok
    assert backend.sent == []


# -- send validation -------------------------------------------------------


async def test_send_requires_a_body(backend: InMemoryMailBackend) -> None:
    """An empty body is almost always a model error, not an intention."""
    gateway, governor = await build_gateway(backend)
    held = await gateway.call(ALICE, "mail.send_reply", {"to": ["a@corp.example"], "body": " "})

    # Held first; validation happens when it actually runs.
    context = await governor.approve(held.pending_action_id)
    call = await gateway.call(
        ALICE, "mail.send_reply", {"to": ["a@corp.example"], "body": " "}, context=context
    )

    assert not call.ok
    assert "body is required" in (call.result.error or "")


async def test_single_recipient_string_is_accepted(backend: InMemoryMailBackend) -> None:
    """Models routinely send a bare string where the schema declares an array."""
    gateway, governor = await build_gateway(backend)
    held = await gateway.call(ALICE, "mail.send_reply", {"to": "cfo@corp.example", "body": "hi"})
    context = await governor.approve(held.pending_action_id)

    call = await gateway.call(
        ALICE,
        "mail.send_reply",
        {"to": "cfo@corp.example", "body": "hi"},
        context=context,
    )

    assert call.ok
    assert backend.sent[0]["to"] == ["cfo@corp.example"]


async def test_reply_threading_is_preserved(backend: InMemoryMailBackend) -> None:
    gateway, governor = await build_gateway(backend)
    args = {"to": ["cfo@corp.example"], "body": "hi", "in_reply_to": "<abc@corp.example>"}
    held = await gateway.call(ALICE, "mail.send_reply", args)
    context = await governor.approve(held.pending_action_id)

    await gateway.call(ALICE, "mail.send_reply", args, context=context)

    assert backend.sent[0]["in_reply_to"] == "<abc@corp.example>"


# -- undo ------------------------------------------------------------------


async def test_mark_read_registers_an_undo(backend: InMemoryMailBackend) -> None:
    gateway, governor = await build_gateway(backend)
    register_mail_undo(governor.journal)
    held = await gateway.call(ALICE, "mail.mark_read", {"uid": "1"})
    context = await governor.approve(held.pending_action_id)

    await gateway.call(ALICE, "mail.mark_read", {"uid": "1"}, context=context)

    entry = (await governor.journal.recent_for(ALICE))[0]
    assert entry.reversible
    assert entry.undo_arguments == {"uid": "1"}


async def test_sent_mail_is_never_claimed_reversible(backend: InMemoryMailBackend) -> None:
    """You cannot unsend an email; the journal must not pretend otherwise."""
    gateway, governor = await build_gateway(backend)
    register_mail_undo(governor.journal)
    args = {"to": ["cfo@corp.example"], "body": "hi"}
    held = await gateway.call(ALICE, "mail.send_reply", args)
    context = await governor.approve(held.pending_action_id)

    await gateway.call(ALICE, "mail.send_reply", args, context=context)

    assert not (await governor.journal.recent_for(ALICE))[0].reversible
