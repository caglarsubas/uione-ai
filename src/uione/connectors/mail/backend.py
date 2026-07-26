"""Mail backend protocol and an in-memory implementation.

The MCP tool layer talks to this protocol, never to ``imaplib``. That keeps the
tool wiring testable without a mail server and confines protocol quirks — IMAP's
1-indexed sequence numbers, its two different search dialects, its habit of
returning ``None`` for a fetch that "succeeded" — to one file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from uione.connectors.mail.message import MailMessage


class MailError(RuntimeError):
    """Any failure reaching or reading the mail server."""


@dataclass
class MailAccount:
    """Connection settings for one mailbox.

    Credentials arrive from the secrets manager, never from a prompt or a model
    (F3.5). This object should be short-lived.
    """

    host: str
    username: str
    password: str = ""
    port: int = 993
    use_ssl: bool = True
    mailbox: str = "INBOX"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_use_tls: bool = True

    #: Domains treated as inside the organisation, for trust classification.
    internal_domains: frozenset[str] = frozenset()

    timeout_s: float = 30.0

    @property
    def resolved_smtp_host(self) -> str:
        return self.smtp_host or self.host


class MailBackend(Protocol):
    """What the mail tools need from a mail system."""

    async def list_unread(self, limit: int = 20) -> list[MailMessage]: ...

    async def search(self, query: str, *, limit: int = 20) -> list[MailMessage]: ...

    async def get_message(self, uid: str) -> MailMessage | None: ...

    async def mark_read(self, uid: str) -> None: ...

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> str: ...


@dataclass
class InMemoryMailBackend:
    """A mailbox in a dict. Used by tests and offline demos."""

    messages: list[MailMessage] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    fail_with: str | None = None

    def _guard(self) -> None:
        if self.fail_with:
            raise MailError(self.fail_with)

    async def list_unread(self, limit: int = 20) -> list[MailMessage]:
        self._guard()
        unread = [m for m in self.messages if m.unread]
        return sorted(unread, key=_sort_key, reverse=True)[:limit]

    async def search(self, query: str, *, limit: int = 20) -> list[MailMessage]:
        self._guard()
        needle = query.lower()
        hits = [
            m
            for m in self.messages
            if needle in m.subject.lower()
            or needle in m.body.lower()
            or needle in m.from_address.lower()
        ]
        return sorted(hits, key=_sort_key, reverse=True)[:limit]

    async def get_message(self, uid: str) -> MailMessage | None:
        self._guard()
        return next((m for m in self.messages if m.uid == uid), None)

    async def mark_read(self, uid: str) -> None:
        self._guard()
        for message in self.messages:
            if message.uid == uid:
                message.unread = False
                return
        raise MailError(f"no such message: {uid}")

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> str:
        self._guard()
        self.sent.append(
            {
                "to": to,
                "cc": cc or [],
                "subject": subject,
                "body": body,
                "in_reply_to": in_reply_to,
            }
        )
        return f"<generated-{len(self.sent)}@uione.local>"


def _sort_key(message: MailMessage):
    """Sort newest first, tolerating messages with no parseable date."""
    return (message.date is not None, message.date)
