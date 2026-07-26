"""Mail exposed as governed MCP tools.

Risk classification is explicit rather than inherited from any server's own
annotations, per the connector certification rule (F3.8):

* reads are ``READ`` but declare ``returns_untrusted_content`` — anyone on the
  internet can write into this mailbox, so reading it taints the session
* ``mark_read`` is a reversible write
* ``send_reply`` is ``EXTERNAL_FACING``: it leaves the organisation, so it is
  subject to egress checks and can never earn unattended execution while
  untrusted content is in context
"""

from __future__ import annotations

import structlog

from uione.connectors.mail.backend import MailBackend, MailError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 15
MAX_LIMIT = 50


def _clamp(value: object, default: int = DEFAULT_LIMIT) -> int:
    """Bound a model-supplied limit.

    A model that asks for 10,000 messages is not malicious, just optimistic, and
    the result would blow the context budget and the mail server's patience alike.
    """
    try:
        requested = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(requested, MAX_LIMIT))


def build_mail_source(backend: MailBackend, *, name: str = "mail") -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def list_unread(args: dict) -> ToolResult:
        try:
            messages = await backend.list_unread(limit=_clamp(args.get("limit")))
        except MailError as exc:
            return ToolResult.failure(str(exc))

        if not messages:
            return ToolResult.success("No unread messages.", {"count": 0})
        return ToolResult.success(
            "\n".join(m.render() for m in messages),
            {
                "count": len(messages),
                "external_senders": sum(1 for m in messages if m.external),
            },
        )

    async def search(args: dict) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.failure("query is required and must not be empty")
        try:
            messages = await backend.search(query, limit=_clamp(args.get("limit")))
        except MailError as exc:
            return ToolResult.failure(str(exc))

        if not messages:
            return ToolResult.success(f"No messages matching {query!r}.", {"count": 0})
        return ToolResult.success("\n".join(m.render() for m in messages), {"count": len(messages)})

    async def get_message(args: dict) -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        if not uid:
            return ToolResult.failure("uid is required")
        try:
            message = await backend.get_message(uid)
        except MailError as exc:
            return ToolResult.failure(str(exc))

        if message is None:
            return ToolResult.failure(f"no message with id {uid!r}")
        return ToolResult.success(
            message.render(body_chars=4000),
            {"external": message.external, "message_id": message.message_id},
        )

    async def mark_read(args: dict) -> ToolResult:
        uid = str(args.get("uid", "")).strip()
        if not uid:
            return ToolResult.failure("uid is required")
        try:
            await backend.mark_read(uid)
        except MailError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(f"Message {uid} marked as read.")

    async def send_reply(args: dict) -> ToolResult:
        recipients = args.get("to")
        if isinstance(recipients, str):
            recipients = [recipients]
        if not recipients:
            return ToolResult.failure("to is required")

        body = str(args.get("body", "")).strip()
        if not body:
            return ToolResult.failure("body is required and must not be empty")

        cc = args.get("cc")
        if isinstance(cc, str):
            cc = [cc]

        try:
            message_id = await backend.send(
                to=[str(r) for r in recipients],
                subject=str(args.get("subject", "")),
                body=body,
                cc=[str(c) for c in cc] if cc else None,
                in_reply_to=args.get("in_reply_to"),
            )
        except MailError as exc:
            return ToolResult.failure(str(exc))
        return ToolResult.success(
            f"Sent to {', '.join(str(r) for r in recipients)}.", {"message_id": message_id}
        )

    source.register(
        "list_unread",
        list_unread,
        description="List unread messages in the user's mailbox, newest first.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"Maximum messages to return (1-{MAX_LIMIT}).",
                }
            },
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "search",
        search,
        description="Search the mailbox for messages matching text.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for."},
                "limit": {"type": "integer", "description": f"Maximum results (1-{MAX_LIMIT})."},
            },
            "required": ["query"],
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "get_message",
        get_message,
        description="Read one message in full by its id.",
        parameters={
            "type": "object",
            "properties": {"uid": {"type": "string", "description": "Message id."}},
            "required": ["uid"],
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "mark_read",
        mark_read,
        description="Mark a message as read.",
        parameters={
            "type": "object",
            "properties": {"uid": {"type": "string"}},
            "required": ["uid"],
        },
        risk=RiskClass.REVERSIBLE_WRITE,
    )
    source.register(
        "send_reply",
        send_reply,
        description="Send an email on the user's behalf.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "in_reply_to": {
                    "type": "string",
                    "description": "Message-ID being replied to, if any.",
                },
            },
            "required": ["to", "body"],
        },
        risk=RiskClass.EXTERNAL_FACING,
    )
    return source


def register_mail_undo(journal) -> None:
    """Teach the journal how to reverse mail writes.

    Only ``mark_read`` is reversible. A sent message is not, which is precisely
    why it is classified ``EXTERNAL_FACING`` and gated rather than journalled and
    hoped about.
    """
    journal.register_undo(
        "mail.mark_read",
        lambda args, _result: ("mail.mark_unread", {"uid": args["uid"]}),
    )
