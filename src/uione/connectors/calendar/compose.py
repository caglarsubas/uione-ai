"""Writing an iCalendar event that a real server will accept.

Reading iCalendar is forgiving; writing it is not. Servers reject what they will
happily have produced themselves, and the rejections are unhelpful — a `400` with
no body is common. So this file is small and pedantic, and every rule below is
one that RFC 5545 states and that something in the wild enforces.

**TEXT values are escaped.** A comma, semicolon or backslash in a summary is
structural syntax unless escaped, so "Review budget, headcount and hiring"
either fails to parse or silently becomes two properties. Newlines become `\\n`.

**Lines are folded at 75 octets.** Not characters — octets, so a summary with
accented characters folds earlier than its length suggests. Folding inserts CRLF
followed by a single space, and the space is part of the protocol rather than
indentation.

**CRLF, everywhere.** RFC 5545 requires it. Plenty of parsers accept bare LF,
which is exactly what makes this the sort of bug that appears only against the
one server that does not.

**UID is ours and it is stable.** It goes in the filename we PUT to, so a
generated UID is the difference between creating an event and overwriting one.

**No attendee is invented.** Every address in the output was passed in. A model
that hallucinates a plausible colleague's address must not have that address
posted to a calendar server that will then email them.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

#: Maximum octets per line before folding, per RFC 5545 §3.1.
LINE_OCTETS = 75

#: Characters that are structural in a TEXT value and must be escaped.
_ESCAPES = {"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n", "\r": ""}

_ADDRESS = re.compile(r"^[^@\s,;:]+@[^@\s,;:]+\.[^@\s,;:]+$")


def escape_text(value: str) -> str:
    """Escape a TEXT value.

    Backslash first, or the escapes we add would themselves be escaped.
    """
    out = value.replace("\\", "\\\\")
    for char, replacement in _ESCAPES.items():
        if char == "\\":
            continue
        out = out.replace(char, replacement)
    return out


def fold(line: str) -> str:
    """Fold a content line at 75 octets, continuing with a leading space.

    Measured in octets rather than characters: a summary in Turkish or German
    folds earlier than its length suggests, and a server counting octets will
    reject a line this function thought was short enough.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= LINE_OCTETS:
        return line

    pieces: list[str] = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        # The continuation space costs an octet on every line after the first,
        # so the budget shrinks once folding has begun.
        budget = LINE_OCTETS - (1 if pieces else 0)
        if len(current) + len(char_bytes) > budget:
            pieces.append(current.decode("utf-8"))
            current = b""
        current += char_bytes
    if current:
        pieces.append(current.decode("utf-8"))

    return "\r\n ".join(pieces)


def valid_address(value: str) -> bool:
    return bool(_ADDRESS.match(value.strip()))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_event(
    *,
    summary: str,
    start: datetime,
    end: datetime,
    organizer: str,
    attendees: list[str],
    description: str = "",
    location: str = "",
    uid: str = "",
    now: datetime | None = None,
) -> tuple[str, str]:
    """Build one VEVENT. Returns ``(uid, ics)``.

    Times are written in UTC. A floating local time would be interpreted in the
    *server's* timezone, which is how an 09:00 meeting arrives at 11:00 for
    somebody in another office.
    """
    if end <= start:
        raise ValueError("a meeting must end after it starts")
    if not summary.strip():
        raise ValueError("a meeting needs a title")

    bad = [a for a in attendees if not valid_address(a)]
    if bad:
        raise ValueError(f"not valid email addresses: {', '.join(bad)}")

    event_uid = uid or f"{uuid.uuid4()}@uione"
    stamp = _stamp(now or datetime.now(UTC))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//UiOne//Assistant//EN",
        "CALSCALE:GREGORIAN",
        # REQUEST rather than PUBLISH: this is an invitation that expects
        # replies, and a server treating it as a publication will not collect
        # them.
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{event_uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{escape_text(summary.strip())}",
        f"ORGANIZER;CN={escape_text(organizer.split('@')[0])}:mailto:{organizer}",
        # Tentative until people answer. Marking it CONFIRMED would show a
        # meeting as settled in everyone's calendar before anyone accepted.
        "STATUS:TENTATIVE",
        "SEQUENCE:0",
        "TRANSP:OPAQUE",
    ]
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    if description:
        lines.append(f"DESCRIPTION:{escape_text(description)}")
    for attendee in attendees:
        lines.append(
            "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee.strip()}"
        )
    lines += ["END:VEVENT", "END:VCALENDAR"]

    # CRLF because the RFC says so. Plenty of parsers accept bare LF, which is
    # what makes this the bug that only shows up against the one that does not.
    return event_uid, "\r\n".join(fold(line) for line in lines) + "\r\n"
