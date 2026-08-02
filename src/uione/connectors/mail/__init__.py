"""Mail connector — IMAP/SMTP, the critical-path Wave-1 integration."""

from uione.connectors.mail.backend import (
    InMemoryMailBackend,
    MailAccount,
    MailBackend,
    MailError,
)
from uione.connectors.mail.imap_backend import ImapMailBackend, quote_imap
from uione.connectors.mail.message import (
    Attachment,
    MailMessage,
    decode_mime_header,
    extract_body,
    html_to_text,
    is_external,
    parse_message,
)
from uione.connectors.mail.source import (
    build_mail_source,
    register_mail_undo,
    register_mail_verification,
)

__all__ = [
    "Attachment",
    "ImapMailBackend",
    "InMemoryMailBackend",
    "MailAccount",
    "MailBackend",
    "MailError",
    "MailMessage",
    "build_mail_source",
    "decode_mime_header",
    "extract_body",
    "html_to_text",
    "is_external",
    "parse_message",
    "quote_imap",
    "register_mail_undo",
    "register_mail_verification",
]
