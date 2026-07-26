"""Documents and their access control.

Gap **G3**. The failure this exists to prevent is concrete and career-ending: the
assistant surfacing the layoff plan to an intern because the index knew the
document existed and nobody asked whether that person was allowed to see it.

Two rules carry the weight:

**Deny by default.** A document with no ACL is visible to *nobody*, not to
everybody. An ingestion bug that loses permissions must make content disappear,
which someone notices and reports, rather than make it universally readable,
which nobody notices until it is a headline.

**The ACL travels with the document.** Not looked up later, not inferred from a
folder name — carried on the record, so there is no window in which the content
is indexed and the permissions are not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from uione.mcphub import Principal


class Visibility(StrEnum):
    RESTRICTED = "restricted"
    """Only the principals and groups named in the ACL. The default."""

    ORGANISATION = "organisation"
    """Anyone authenticated in this organisation. Must be set deliberately."""


@dataclass(frozen=True)
class AccessControl:
    """Who may read a document, mirrored from the source system.

    Mirrored rather than invented: whatever SharePoint, Confluence or the file
    share says is what applies here. An index that maintains its own opinion of
    permissions will drift, and drift in this direction is a breach.
    """

    users: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    visibility: Visibility = Visibility.RESTRICTED

    #: Principals explicitly excluded, whatever else grants them access.
    #: Source systems have deny rules and dropping them silently widens access.
    denied_users: frozenset[str] = frozenset()

    @classmethod
    def organisation_wide(cls) -> AccessControl:
        return cls(visibility=Visibility.ORGANISATION)

    @classmethod
    def for_users(cls, *user_ids: str) -> AccessControl:
        return cls(users=frozenset(user_ids))

    @classmethod
    def for_groups(cls, *groups: str) -> AccessControl:
        return cls(groups=frozenset(groups))

    @property
    def empty(self) -> bool:
        return not self.users and not self.groups and self.visibility is Visibility.RESTRICTED

    def permits(self, principal: Principal) -> bool:
        """Whether this principal may read the document.

        Denials are checked first and win outright. Everything else is additive.
        """
        if principal.user_id in self.denied_users:
            return False
        if self.visibility is Visibility.ORGANISATION:
            return True
        if principal.user_id in self.users:
            return True
        return bool(self.groups & principal.roles)

    def fingerprint(self) -> str:
        """Stable hash of the ACL, for detecting drift against the source."""
        parts = [
            ",".join(sorted(self.users)),
            ",".join(sorted(self.groups)),
            ",".join(sorted(self.denied_users)),
            self.visibility.value,
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class Document:
    """One indexed item, with the permissions it came with."""

    id: str
    title: str
    body: str
    source: str
    """Qualified tool that produced it — provenance, and the unit of re-sync."""

    acl: AccessControl = field(default_factory=AccessControl)
    url: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"

    @property
    def readable_by_nobody(self) -> bool:
        """True when this document is indexed but reachable by no one.

        Not an error — it is the safe outcome of an ACL we could not resolve —
        but it is worth surfacing, because a whole source in this state means a
        broken permission sync rather than a genuinely private corpus.
        """
        return self.acl.empty

    def snippet(self, query: str = "", *, length: int = 240) -> str:
        """A short extract, centred on the query when one matches."""
        body = self.body.strip().replace("\n", " ")
        if not query:
            return body[:length] + ("…" if len(body) > length else "")

        position = body.lower().find(query.lower().split()[0]) if query.split() else -1
        if position == -1:
            return body[:length] + ("…" if len(body) > length else "")

        start = max(0, position - length // 3)
        extract = body[start : start + length]
        return ("…" if start else "") + extract + ("…" if start + length < len(body) else "")
