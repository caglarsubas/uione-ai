"""IMAP/SMTP backend.

Built on the standard library rather than an async IMAP package: this ships into
air-gapped estates, and every dependency is a thing the customer's security team
must review and we must patch. ``imaplib`` and ``smtplib`` are synchronous, so
calls run in a worker thread.

Two decisions worth knowing about:

**UIDs, not sequence numbers.** Every command uses the UID variants. Sequence
numbers are renumbered when anything is expunged, so a brief that reads message 4
and later marks message 4 as read can easily mark a *different* message. UIDs are
stable for the life of the mailbox.

**A connection per operation.** IMAP connections are stateful and not thread-safe,
and a pooled connection that silently drops is a class of bug that shows up only
under load. Login costs a few hundred milliseconds; the brief makes one or two
calls. Pooling is a later optimisation with a real benchmark behind it, not a
guess made now.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import structlog

from uione.connectors.mail.backend import MailAccount, MailError
from uione.connectors.mail.message import MailMessage, parse_message

log = structlog.get_logger(__name__)

#: IMAP protocol limit is 8192 for a command line; keep queries well inside it.
_MAX_QUERY_CHARS = 512


def quote_imap(value: str) -> str:
    r"""Quote a string for an IMAP command.

    The search term originates from a model, which means it is attacker-
    influenced whenever untrusted content is in context. An unescaped ``"`` would
    end the quoted string and let the rest of the value be read as IMAP command
    tokens — the mail equivalent of SQL injection. Backslash and quote are the
    only characters IMAP quoted-strings treat specially.
    """
    trimmed = value[:_MAX_QUERY_CHARS]
    escaped = trimmed.replace("\\", "\\\\").replace('"', '\\"')
    # CR/LF would terminate the command line entirely.
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return f'"{escaped}"'


class ImapMailBackend:
    """Reads over IMAP, sends over SMTP."""

    def __init__(self, account: MailAccount) -> None:
        self._account = account

    # -- connection --------------------------------------------------------

    def _connect(self) -> imaplib.IMAP4:
        account = self._account
        try:
            if account.use_ssl:
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    account.host, account.port, timeout=account.timeout_s
                )
            else:
                client = imaplib.IMAP4(account.host, account.port, timeout=account.timeout_s)
            client.login(account.username, account.password)
        except (OSError, imaplib.IMAP4.error) as exc:
            # Never surface the exception text verbatim: imaplib includes the
            # attempted credentials in some failure modes.
            raise MailError(f"could not connect to mail server: {type(exc).__name__}") from exc
        return client

    def _with_connection(self, fn, *, readonly: bool = True):
        client = self._connect()
        try:
            typ, _ = client.select(self._account.mailbox, readonly=readonly)
            if typ != "OK":
                raise MailError(f"could not open mailbox {self._account.mailbox!r}")
            return fn(client)
        finally:
            # Both are best-effort: the operation already succeeded or failed on
            # its own terms, and a teardown error must not mask that outcome.
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client.close()
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client.logout()

    # -- reads -------------------------------------------------------------

    def _search_uids(self, client: imaplib.IMAP4, criteria: list[str]) -> list[bytes]:
        typ, data = client.uid("SEARCH", None, *criteria)
        if typ != "OK" or not data or data[0] is None:
            return []
        return data[0].split()

    def _fetch(self, client: imaplib.IMAP4, uids: list[bytes], limit: int) -> list[MailMessage]:
        messages: list[MailMessage] = []
        # Newest first: IMAP returns ascending UIDs and recency tracks UID order.
        for raw_uid in reversed(uids[-limit:] if limit else uids):
            uid = raw_uid.decode()
            typ, data = client.uid("FETCH", uid, "(RFC822 FLAGS)")
            if typ != "OK" or not data or not data[0]:
                log.debug("mail.fetch_missed", uid=uid)
                continue

            envelope = data[0]
            if not isinstance(envelope, tuple) or len(envelope) < 2:
                continue

            flags_blob = envelope[0] if isinstance(envelope[0], bytes) else b""
            messages.append(
                parse_message(
                    envelope[1],
                    uid=uid,
                    unread=b"\\Seen" not in flags_blob,
                    internal_domains=self._account.internal_domains,
                )
            )
        return messages

    async def list_unread(self, limit: int = 20) -> list[MailMessage]:
        def run(client: imaplib.IMAP4) -> list[MailMessage]:
            return self._fetch(client, self._search_uids(client, ["UNSEEN"]), limit)

        return await asyncio.to_thread(self._with_connection, run)

    async def search(self, query: str, *, limit: int = 20) -> list[MailMessage]:
        quoted = quote_imap(query)

        def run(client: imaplib.IMAP4) -> list[MailMessage]:
            uids = self._search_uids(client, ["TEXT", quoted])
            return self._fetch(client, uids, limit)

        return await asyncio.to_thread(self._with_connection, run)

    async def get_message(self, uid: str) -> MailMessage | None:
        safe_uid = uid.strip()
        if not safe_uid.isdigit():
            # UIDs are numeric; anything else is a model mistake or an injection.
            raise MailError(f"invalid message id: {uid!r}")

        def run(client: imaplib.IMAP4) -> list[MailMessage]:
            return self._fetch(client, [safe_uid.encode()], 1)

        found = await asyncio.to_thread(self._with_connection, run)
        return found[0] if found else None

    # -- writes ------------------------------------------------------------

    async def mark_read(self, uid: str) -> None:
        safe_uid = uid.strip()
        if not safe_uid.isdigit():
            raise MailError(f"invalid message id: {uid!r}")

        def run(client: imaplib.IMAP4) -> None:
            typ, _ = client.uid("STORE", safe_uid, "+FLAGS", "(\\Seen)")
            if typ != "OK":
                raise MailError(f"could not mark {uid} as read")

        await asyncio.to_thread(self._with_connection, run, readonly=False)

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
    ) -> str:
        account = self._account
        message = EmailMessage()
        message["From"] = account.username
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message_id = make_msgid(domain="uione.local")
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(body)

        def run() -> None:
            try:
                with smtplib.SMTP(
                    account.resolved_smtp_host, account.smtp_port, timeout=account.timeout_s
                ) as server:
                    if account.smtp_use_tls:
                        server.starttls()
                    if account.password:
                        server.login(account.username, account.password)
                    server.send_message(message)
            except (OSError, smtplib.SMTPException) as exc:
                raise MailError(f"could not send mail: {type(exc).__name__}") from exc

        await asyncio.to_thread(run)
        log.info("mail.sent", to=to, subject=subject[:60], message_id=message_id)
        return message_id
