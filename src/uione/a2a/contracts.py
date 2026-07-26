"""Disclosure contracts — gap G9.

"My assistant talks to your assistant" is a data-leak generator unless something
decides *what mine is allowed to tell yours*. Left ungoverned, the natural
implementation is an assistant that helpfully answers every question it can, and
the first time someone asks a colleague's assistant "what is she working on?" it
will say, in detail.

A contract answers one question: **for this requester, what facets of my owner's
working life may be revealed?** Everything not granted is withheld, and the
withholding is *reported* rather than silently trimmed — an answer that quietly
omits half the picture is worse than one that says what it is not showing.

Facets are deliberately coarse. Fine-grained per-field rules read as thorough and
are unusable: nobody configures forty toggles, so everyone accepts the default,
and the default is what actually ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Facet(StrEnum):
    """A kind of information one assistant may reveal to another."""

    FREE_BUSY = "free_busy"
    """When the owner is free. Times only — never what the meetings are."""

    MEETING_SUBJECTS = "meeting_subjects"
    """What the meetings are about. Rarely granted outside a close team."""

    WORKLOAD = "workload"
    """Rough capacity: "heavily loaded this week". No specifics."""

    TASK_STATUS = "task_status"
    """Status of work items the requester can already see in the tracker."""

    TASK_DETAIL = "task_detail"
    """Titles and content of work items."""

    OUT_OF_OFFICE = "out_of_office"
    """Absence dates. Usually safe; occasionally sensitive (medical leave)."""

    CONTACT = "contact"
    """Working hours, timezone, preferred channel."""


#: What a colleague in the same organisation gets by default.
#: Times but not subjects, capacity but not content. This is the line most
#: people would draw for a colleague they do not work with directly, and the
#: default is what will actually be deployed.
DEFAULT_INTERNAL = frozenset({Facet.FREE_BUSY, Facet.OUT_OF_OFFICE, Facet.CONTACT})

#: A close team sees more, because the alternative is that they route everything
#: through the humans and stop using the assistant.
DEFAULT_TEAM = DEFAULT_INTERNAL | {Facet.WORKLOAD, Facet.TASK_STATUS}

#: Anyone outside the organisation. Deliberately empty: an external assistant
#: gets nothing until someone decides otherwise.
DEFAULT_EXTERNAL: frozenset[Facet] = frozenset()


@dataclass
class DisclosureContract:
    """What one person's assistant may reveal, and to whom.

    Owned by the *subject*, not the requester. Bob decides what Bob's assistant
    says about Bob — which is the only arrangement an employee would accept, and
    the one a works council will ask about.
    """

    owner_id: str
    by_role: dict[str, frozenset[Facet]] = field(default_factory=dict)
    by_user: dict[str, frozenset[Facet]] = field(default_factory=dict)
    default: frozenset[Facet] = DEFAULT_INTERNAL
    external_default: frozenset[Facet] = DEFAULT_EXTERNAL

    def facets_for(
        self, requester_id: str, roles: frozenset[str], *, external: bool = False
    ) -> frozenset[Facet]:
        """Everything this requester may see.

        A per-user grant wins over roles, and roles union together. Union rather
        than intersection because roles are additive in every directory system
        people actually run: someone in both "team" and "oncall" should see what
        either grants, not only what both do.
        """
        if requester_id in self.by_user:
            return self.by_user[requester_id]

        granted = frozenset()
        for role in roles:
            granted |= self.by_role.get(role, frozenset())

        if granted:
            return granted
        return self.external_default if external else self.default

    def grant(
        self, *, role: str | None = None, user: str | None = None, facets: frozenset[Facet]
    ) -> None:
        if user:
            self.by_user[user] = facets
        if role:
            self.by_role[role] = facets

    def revoke(self, *, user: str) -> None:
        """Explicitly deny one person, overriding any role grant."""
        self.by_user[user] = frozenset()


@dataclass(frozen=True)
class Disclosure:
    """The result of applying a contract: what is shared, and what was not.

    ``withheld`` exists so the answer can say so. An assistant that returns a
    partial answer without indicating it is partial teaches the asker to treat
    incomplete information as complete.
    """

    granted: frozenset[Facet]
    withheld: frozenset[Facet]

    @property
    def is_empty(self) -> bool:
        return not self.granted

    def allows(self, facet: Facet) -> bool:
        return facet in self.granted

    def explain_withheld(self) -> str:
        if not self.withheld:
            return ""
        names = ", ".join(sorted(f.value.replace("_", " ") for f in self.withheld))
        return f"withheld by {'their' if self.granted else 'the owner'} disclosure policy: {names}"


class ContractRegistry:
    """Everyone's contracts.

    A missing contract yields the default rather than an error: an employee who
    has never opened the settings still has a defensible policy, and it is the
    conservative one.
    """

    def __init__(self) -> None:
        self._contracts: dict[str, DisclosureContract] = {}

    def for_owner(self, owner_id: str) -> DisclosureContract:
        return self._contracts.setdefault(owner_id, DisclosureContract(owner_id=owner_id))

    def set(self, contract: DisclosureContract) -> None:
        self._contracts[contract.owner_id] = contract

    def evaluate(
        self,
        *,
        owner_id: str,
        requester_id: str,
        requester_roles: frozenset[str],
        requested: frozenset[Facet],
        external: bool = False,
    ) -> Disclosure:
        allowed = self.for_owner(owner_id).facets_for(
            requester_id, requester_roles, external=external
        )
        granted = requested & allowed
        return Disclosure(granted=granted, withheld=requested - granted)
