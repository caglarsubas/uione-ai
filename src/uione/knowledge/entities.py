"""Entities and the items that mention them.

The work graph's job is to answer "these five things are the same thing" across
systems that share no identifiers by design. Everything starts from a normalised
:class:`EntityRef`: two references to ``PAY-1182`` from a mailbox and a tracker
must produce the same key, or nothing links.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class EntityKind(StrEnum):
    PERSON = "person"
    TICKET = "ticket"
    INCIDENT = "incident"
    MESSAGE = "message"
    MEETING = "meeting"
    DOCUMENT = "document"

    REFERENCE = "reference"
    """A business identifier that is not a record in any one system — an invoice
    number, a claim number, a customer reference. These are the highest-value
    links precisely because no single system owns them, so nothing else joins on
    them."""


@dataclass(frozen=True)
class EntityRef:
    kind: EntityKind
    key: str
    label: str = ""

    @property
    def id(self) -> str:
        return f"{self.kind.value}:{self.key}"

    def __str__(self) -> str:
        return self.label or self.key


def normalise_key(kind: EntityKind, raw: str) -> str:
    """Canonical form for an entity key.

    Case folding is the whole game here: a mailbox writes ``pay-1182``, a tracker
    writes ``PAY-1182``, and if those do not collapse to one key the graph
    silently contains two unrelated nodes and links nothing.
    """
    cleaned = raw.strip()
    if kind is EntityKind.PERSON:
        return cleaned.lower()
    if kind in (EntityKind.TICKET, EntityKind.INCIDENT, EntityKind.REFERENCE):
        return cleaned.upper()
    return cleaned


def entity(kind: EntityKind, raw: str, label: str = "") -> EntityRef:
    return EntityRef(kind=kind, key=normalise_key(kind, raw), label=label or raw.strip())


@dataclass
class GraphItem:
    """One record from one system, and what it refers to.

    ``subject`` is the thing this item *is* (a ticket, a message); ``mentions``
    are the things it *refers to*. Keeping the distinction is what lets the graph
    answer "what is about INC-4471" differently from "what is INC-4471".
    """

    source: str
    """Qualified tool that produced it, e.g. ``mail.list_unread``. Provenance."""

    subject: EntityRef
    title: str = ""
    body: str = ""
    at: datetime | None = None
    mentions: set[EntityRef] = field(default_factory=set)
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.subject.id

    def summary(self) -> str:
        return self.title or self.body[:80] or self.subject.id
