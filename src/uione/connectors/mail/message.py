"""Mail message model and MIME parsing.

Real mailboxes are hostile input: RFC 2047 encoded headers in half a dozen
charsets, multipart trees nested three deep, HTML-only messages, declared
encodings that lie, and attachments that must never be silently loaded into a
prompt. Parsing is therefore total — every branch produces a message, and
anything unreadable degrades to a marker rather than raising.

A parser that throws on the one malformed message in an inbox takes the whole
morning brief down with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

import structlog

log = structlog.get_logger(__name__)

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]*\n\s*\n\s*")
_STYLE_BLOCK = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.I)


@dataclass
class Attachment:
    """Attachment metadata only.

    Contents are deliberately not read. An attachment is untrusted bytes of
    unknown size; pulling one into a prompt is both a token disaster and an
    injection vector. Document understanding is a separate, explicit pipeline
    (F1.7), not something that happens implicitly during a mail read.
    """

    filename: str
    content_type: str
    size_bytes: int | None = None


@dataclass
class MailMessage:
    uid: str
    subject: str = ""
    from_address: str = ""
    from_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date: datetime | None = None
    body: str = ""
    unread: bool = True
    attachments: list[Attachment] = field(default_factory=list)
    message_id: str = ""
    in_reply_to: str = ""

    #: Whether the sender is outside the organisation. Drives trust
    #: classification, so it is computed from configuration rather than guessed.
    external: bool = True

    @property
    def sender_display(self) -> str:
        if self.from_name and self.from_address:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address or self.from_name or "(unknown sender)"

    def render(self, *, body_chars: int = 600) -> str:
        """Render for a model prompt.

        Bodies are truncated because an unbounded mailbox read is how a brief
        blows its context budget; the truncation is stated so the model knows it
        is looking at part of a message.
        """
        lines = [
            f"[{self.uid}] {self.date.strftime('%Y-%m-%d %H:%M') if self.date else 'unknown date'}"
            f" from {self.sender_display}" + (" (EXTERNAL SENDER)" if self.external else ""),
            f"    Subject: {self.subject or '(no subject)'}",
        ]
        body = self.body.strip()
        if len(body) > body_chars:
            body = body[:body_chars] + f"… [truncated, {len(self.body) - body_chars} more chars]"
        if body:
            lines.append("    " + body.replace("\n", "\n    "))
        if self.attachments:
            names = ", ".join(f"{a.filename} ({a.content_type})" for a in self.attachments)
            lines.append(f"    Attachments (not read): {names}")
        return "\n".join(lines)


def decode_mime_header(raw: str | None) -> str:
    """Decode an RFC 2047 header to text.

    Falls back to the raw value rather than raising: a subject line with a
    broken charset is still worth showing.
    """
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        log.debug("mail.header_decode_failed", raw=raw[:80])
        return raw.strip()


def _decode_payload(part: Message) -> str:
    """Decode one part's payload to text, tolerating lying charset declarations."""
    try:
        payload = part.get_payload(decode=True)
    except (AssertionError, ValueError, TypeError):
        return ""
    if payload is None:
        return ""

    charset = part.get_content_charset() or "utf-8"
    for candidate in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(candidate, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort: never lose the message entirely over an encoding problem.
    return payload.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Flatten HTML to readable text.

    Deliberately crude. This text goes to a model for summarisation, not to a
    renderer, so structure matters far less than not carrying markup and script
    contents into the prompt.
    """
    without_blocks = _STYLE_BLOCK.sub(" ", html)
    text = _HTML_TAG.sub(" ", without_blocks)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [line.strip() for line in text.splitlines()]
    return _WHITESPACE.sub("\n\n", "\n".join(line for line in lines if line))


def extract_body(message: Message) -> tuple[str, list[Attachment]]:
    """Pull the best-effort text body and attachment metadata from a MIME tree.

    ``text/plain`` wins over ``text/html`` when both are present, which is the
    common ``multipart/alternative`` case and gives cleaner text for the model.
    """
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []

    if not message.is_multipart():
        content = _decode_payload(message)
        if message.get_content_type() == "text/html":
            return html_to_text(content), []
        return content.strip(), []

    for part in message.walk():
        if part.is_multipart():
            continue

        disposition = (part.get_content_disposition() or "").lower()
        content_type = part.get_content_type()

        if disposition == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True)
            attachments.append(
                Attachment(
                    filename=decode_mime_header(part.get_filename()) or "(unnamed)",
                    content_type=content_type,
                    size_bytes=len(payload) if payload else None,
                )
            )
            continue

        if content_type == "text/plain":
            plain.append(_decode_payload(part))
        elif content_type == "text/html":
            html.append(_decode_payload(part))

    if plain:
        body = "\n".join(plain).strip()
    elif html:
        body = html_to_text("\n".join(html))
    else:
        body = ""

    return body, attachments


def _addresses(message: Message, header: str) -> list[str]:
    raw = message.get_all(header, [])
    return [addr for _name, addr in getaddresses(raw) if addr]


def parse_message(
    raw: bytes | Message,
    *,
    uid: str,
    unread: bool = True,
    internal_domains: frozenset[str] = frozenset(),
) -> MailMessage:
    """Parse a raw message into :class:`MailMessage`.

    Never raises. A message that cannot be parsed still yields an object with a
    marker body, because dropping it silently would leave a gap in the brief that
    the user has no way to notice.
    """
    if isinstance(raw, Message):
        message = raw
    else:
        try:
            from email import message_from_bytes

            message = message_from_bytes(raw)
        except Exception:  # noqa: BLE001
            log.warning("mail.parse_failed", uid=uid)
            return MailMessage(uid=uid, subject="(unparseable message)", unread=unread)

    try:
        body, attachments = extract_body(message)
    except Exception:  # noqa: BLE001
        log.warning("mail.body_extraction_failed", uid=uid)
        body, attachments = "(message body could not be read)", []

    from_pairs = getaddresses(message.get_all("From", []))
    from_name, from_address = from_pairs[0] if from_pairs else ("", "")

    date: datetime | None = None
    if raw_date := message.get("Date"):
        try:
            date = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            log.debug("mail.date_unparseable", uid=uid, raw=raw_date)

    return MailMessage(
        uid=uid,
        subject=decode_mime_header(message.get("Subject")),
        from_address=from_address,
        from_name=decode_mime_header(from_name),
        to=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        date=date,
        body=body,
        unread=unread,
        attachments=attachments,
        message_id=(message.get("Message-ID") or "").strip(),
        in_reply_to=(message.get("In-Reply-To") or "").strip(),
        external=is_external(from_address, internal_domains),
    )


def is_external(address: str, internal_domains: frozenset[str]) -> bool:
    """Whether an address is outside the organisation.

    Absent configuration everything is external. Defaulting the other way would
    silently downgrade the trust level of every message in the mailbox.
    """
    if not address or "@" not in address:
        return True
    domain = address.rsplit("@", 1)[1].lower().strip().rstrip(".")
    return not any(domain == d or domain.endswith(f".{d}") for d in internal_domains)
