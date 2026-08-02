"""Parsing tests built from the shapes real mailboxes actually contain."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from uione.connectors.mail import (
    decode_mime_header,
    html_to_text,
    is_external,
    parse_message,
    quote_imap,
)

INTERNAL = frozenset({"corp.example"})


def build(
    *,
    subject: str = "Test",
    sender: str = "someone@corp.example",
    body: str = "Hello",
    html: str | None = None,
    attachment: tuple[str, bytes] | None = None,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "alice@corp.example"
    message["Date"] = "Mon, 27 Jul 2026 08:30:00 +0000"
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


# -- basics ----------------------------------------------------------------


def test_plain_message_is_parsed() -> None:
    parsed = parse_message(build(subject="Budget", body="See attached figures."), uid="7")

    assert parsed.uid == "7"
    assert parsed.subject == "Budget"
    assert "See attached figures." in parsed.body
    assert parsed.from_address == "someone@corp.example"


def test_date_is_parsed() -> None:
    parsed = parse_message(build(), uid="1")
    assert parsed.date is not None
    assert parsed.date.year == 2026


def test_unparseable_date_does_not_fail_the_message() -> None:
    message = EmailMessage()
    message["Subject"] = "x"
    message["From"] = "a@b.example"
    message["Date"] = "not a date"
    message.set_content("body")

    parsed = parse_message(message.as_bytes(), uid="1")

    assert parsed.date is None
    assert parsed.subject == "x"


# -- encoded headers -------------------------------------------------------


def test_rfc2047_header_is_decoded() -> None:
    assert decode_mime_header("=?utf-8?B?QsO8dMOnZQ==?=") == "Bütçe"


def test_turkish_subject_survives_round_trip() -> None:
    parsed = parse_message(build(subject="Bütçe toplantısı ertelendi"), uid="1")
    assert parsed.subject == "Bütçe toplantısı ertelendi"


def test_broken_charset_falls_back_to_raw() -> None:
    """A subject with a bad charset is still worth showing."""
    assert decode_mime_header("=?nonsense-charset?Q?hi?=") != ""


def test_missing_header_is_empty_not_none() -> None:
    assert decode_mime_header(None) == ""


# -- multipart -------------------------------------------------------------


def test_plain_text_wins_over_html_alternative() -> None:
    parsed = parse_message(build(body="plain version", html="<p>html version</p>"), uid="1")
    assert "plain version" in parsed.body
    assert "html version" not in parsed.body


def test_html_only_message_is_flattened() -> None:
    message = EmailMessage()
    message["Subject"] = "HTML"
    message["From"] = "a@b.example"
    message.set_content("<h1>Title</h1><p>Body text</p>", subtype="html")

    parsed = parse_message(message.as_bytes(), uid="1")

    assert "Title" in parsed.body
    assert "<h1>" not in parsed.body


def test_script_and_style_contents_are_stripped() -> None:
    """Script bodies must never reach a prompt as if they were message text."""
    text = html_to_text("<style>.a{color:red}</style><script>alert(1)</script><p>Real</p>")

    assert "Real" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_html_entities_are_decoded() -> None:
    assert "R&D" in html_to_text("<p>R&amp;D budget</p>")


# -- attachments -----------------------------------------------------------


def test_attachment_metadata_is_captured_without_contents() -> None:
    """Attachment bytes are untrusted input of unknown size; never inline them."""
    parsed = parse_message(build(attachment=("invoice.pdf", b"%PDF-1.4 fake")), uid="1")

    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "invoice.pdf"
    assert parsed.attachments[0].size_bytes == len(b"%PDF-1.4 fake")
    assert "%PDF" not in parsed.body


def test_attachments_are_named_in_the_rendering() -> None:
    parsed = parse_message(build(attachment=("invoice.pdf", b"x")), uid="1")
    assert "not read" in parsed.render()


# -- external sender detection --------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("cfo@corp.example", False),
        ("someone@mail.corp.example", False),
        ("attacker@evil.example", True),
        ("attacker@corp.example.evil.example", True),
        ("", True),
        ("malformed", True),
    ],
)
def test_external_detection(address: str, expected: bool) -> None:
    assert is_external(address, INTERNAL) is expected


def test_everything_is_external_without_configuration() -> None:
    """Defaulting to internal would silently downgrade every message's trust."""
    assert is_external("anyone@anywhere.example", frozenset()) is True


def test_external_senders_are_flagged_in_the_rendering() -> None:
    parsed = parse_message(
        build(sender="supplier@outside.example"), uid="1", internal_domains=INTERNAL
    )
    assert "EXTERNAL SENDER" in parsed.render()


def test_internal_senders_are_not_flagged() -> None:
    parsed = parse_message(build(sender="cfo@corp.example"), uid="1", internal_domains=INTERNAL)
    assert "EXTERNAL SENDER" not in parsed.render()


# -- robustness ------------------------------------------------------------


def test_garbage_input_still_yields_a_message() -> None:
    """One malformed message must not take down the whole brief."""
    parsed = parse_message(b"\xff\xfe not a message at all", uid="9")
    assert parsed.uid == "9"


def test_long_body_is_truncated_with_notice() -> None:
    parsed = parse_message(build(body="x" * 5000), uid="1")

    rendered = parsed.render(body_chars=100)

    assert "truncated" in rendered
    assert len(rendered) < 1000


def test_missing_subject_renders_readably() -> None:
    message = EmailMessage()
    message["From"] = "a@b.example"
    message.set_content("body")

    assert "(no subject)" in parse_message(message.as_bytes(), uid="1").render()


# -- IMAP command injection ------------------------------------------------


def test_quote_escapes_embedded_quotes() -> None:
    """The search term comes from a model, which an attacker may have influenced."""
    assert quote_imap('a" (DELETED) "b') == '"a\\" (DELETED) \\"b"'


def test_quote_escapes_backslashes() -> None:
    assert quote_imap("back\\slash") == '"back\\\\slash"'


def test_quote_strips_line_breaks() -> None:
    """CRLF would terminate the IMAP command line and start a new one."""
    quoted = quote_imap("subject\r\nA001 DELETE INBOX")

    assert "\r" not in quoted
    assert "\n" not in quoted


def test_quote_bounds_length() -> None:
    assert len(quote_imap("x" * 10_000)) < 600


# -- bulk detection --------------------------------------------------------


def _msg(headers: dict) -> bytes:
    lines = [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\nbody\r\n").encode()


@pytest.mark.parametrize(
    "header,value",
    [
        ("List-Unsubscribe", "<mailto:x@lists.example>"),
        ("List-Id", "<announce.lists.example>"),
        ("List-Post", "<mailto:announce@lists.example>"),
        ("Auto-Submitted", "auto-generated"),
        ("Auto-Submitted", "auto-replied"),
        ("Precedence", "bulk"),
        ("Precedence", "list"),
        ("Precedence", "junk"),
    ],
)
def test_headers_that_mean_bulk(header: str, value: str) -> None:
    parsed = parse_message(_msg({"From": "a@b.example", header: value}), uid="1")

    assert parsed.bulk


def test_auto_submitted_no_is_not_bulk() -> None:
    """RFC 3834 spells "this is a real message" as Auto-Submitted: no."""
    parsed = parse_message(_msg({"From": "a@b.example", "Auto-Submitted": "no"}), uid="1")

    assert not parsed.bulk


def test_an_ordinary_message_is_not_bulk() -> None:
    """Conservative: an unrecognised message is not bulk. This decides what gets
    hidden from a queue, and hiding a colleague's question is worse than showing
    one newsletter."""
    parsed = parse_message(_msg({"From": "bora@corp.example", "Subject": "Lunch?"}), uid="1")

    assert not parsed.bulk


def test_a_no_reply_address_is_not_itself_bulk() -> None:
    """Matching the address is the tempting wrong answer: it is wrong in both
    directions, and this asserts we did not take it."""
    parsed = parse_message(_msg({"From": "no-reply@corp.example"}), uid="1")

    assert not parsed.bulk
