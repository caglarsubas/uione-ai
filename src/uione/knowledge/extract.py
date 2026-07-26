"""Deterministic entity extraction.

Work graph v1 is deliberately deterministic: shared identifiers, addresses, and
URLs only. No embeddings, no fuzzy matching, no model in the loop.

That is a precision decision, not a shortcut. A wrong link is worse than a
missing one — it puts another customer's ticket in your brief, and a user who
finds one of those stops trusting all of them. Probabilistic resolution (F8.4)
comes later, on top of a base the user can verify by eye.

The same reasoning drives requiring *known* project prefixes by default. A
generic ``[A-Z]{2,}-\\d+`` pattern happily matches ``COVID-19``, ``UTF-8`` and
``ISO-9001``, so it is available but off unless a deployment opts in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from uione.knowledge.entities import EntityKind, EntityRef, entity

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Case-insensitive on purpose. People write "can you look at pay-1182" in email
# constantly, and a case-sensitive pattern would silently drop exactly the
# cross-system links this module exists to find. Precision is enforced by the
# known-prefix allowlist instead, which is a far better filter than letter case.
_GENERIC_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,7})\b")

_MESSAGE_ID = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")

#: Common URL shapes that carry a record identifier.
_URL_KEYS = (
    re.compile(r"/browse/([A-Za-z][A-Za-z0-9]{1,9}-\d{1,7})"),  # Jira
    re.compile(r"/issues/(\d+)"),  # GitLab/GitHub
    re.compile(r"[?&]sys_id=([0-9a-f]{32})"),  # ServiceNow
)


@dataclass
class ExtractionRules:
    """What counts as an identifier in this deployment.

    Every field is configuration because identifier conventions are the most
    customer-specific thing in the product: one estate's ``INC-`` is another's
    ``TICKET-``, and invoice formats are effectively unique per company.
    """

    #: Project prefixes that denote work items, e.g. ``{"PAY", "OPS"}``.
    ticket_prefixes: frozenset[str] = frozenset()

    #: Prefixes that denote incidents rather than ordinary work items.
    incident_prefixes: frozenset[str] = frozenset({"INC"})

    #: Prefixes for business references that no single system owns.
    reference_prefixes: frozenset[str] = frozenset({"INV", "CLM", "PO"})

    #: Accept any ``ABC-123`` shape, not only known prefixes. Off by default:
    #: it matches COVID-19 and ISO-9001 and will manufacture links from them.
    allow_unknown_prefixes: bool = False

    extract_people: bool = True

    #: Domains whose addresses are colleagues rather than outside parties.
    internal_domains: frozenset[str] = frozenset()

    extra_patterns: dict[EntityKind, list[re.Pattern[str]]] = field(default_factory=dict)

    def classify_prefix(self, prefix: str) -> EntityKind | None:
        upper = prefix.upper()
        if upper in self.incident_prefixes:
            return EntityKind.INCIDENT
        if upper in self.reference_prefixes:
            return EntityKind.REFERENCE
        if upper in self.ticket_prefixes:
            return EntityKind.TICKET
        return EntityKind.TICKET if self.allow_unknown_prefixes else None


def extract_entities(text: str, rules: ExtractionRules | None = None) -> set[EntityRef]:
    """Find every entity referenced in a blob of text."""
    rules = rules or ExtractionRules()
    if not text:
        return set()

    found: set[EntityRef] = set()

    for match in _GENERIC_KEY.finditer(text):
        prefix, number = match.group(1), match.group(2)
        kind = rules.classify_prefix(prefix)
        if kind is not None:
            found.add(entity(kind, f"{prefix.upper()}-{number}"))

    for pattern in _URL_KEYS:
        for match in pattern.finditer(text):
            value = match.group(1)
            if key_match := _GENERIC_KEY.fullmatch(value):
                kind = rules.classify_prefix(key_match.group(1))
                if kind is not None:
                    found.add(entity(kind, value))
            else:
                found.add(entity(EntityKind.REFERENCE, value))

    if rules.extract_people:
        for match in _EMAIL.finditer(text):
            address = match.group(0)
            # Message-IDs look like addresses; they are threading metadata, not
            # people, and adding them as colleagues pollutes every person query.
            if f"<{address}>" in text:
                continue
            found.add(entity(EntityKind.PERSON, address))

    for kind, patterns in rules.extra_patterns.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = match.group(1) if match.groups() else match.group(0)
                found.add(entity(kind, value))

    return found


def extract_message_ids(text: str) -> set[str]:
    """RFC 5322 Message-IDs, for mail threading."""
    return {m.group(0) for m in _MESSAGE_ID.finditer(text or "")}
