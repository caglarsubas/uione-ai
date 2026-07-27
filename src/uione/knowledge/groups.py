"""Nested group expansion.

Directories let groups contain groups: ``payments-team`` inside ``engineering``
inside ``all-staff``. A permission granted to ``engineering`` reaches everyone in
``payments-team``, and an index that compares group names literally will deny
people who genuinely have access — or, if it expands carelessly, grant people who
do not.

Three properties, each a real failure in this kind of code:

**Cycles terminate.** ``a`` contains ``b`` contains ``a`` happens in real
directories, usually by accident. A naive recursive expansion hangs, and the
symptom is an authorisation check that never returns.

**Depth is bounded.** Even acyclic nesting can be pathological. A bound turns an
unbounded walk into a stated, testable limit, and exceeding it is reported rather
than silently truncated — a silently truncated expansion *removes* access, which
looks like a permissions bug to the user and is invisible to us.

**Expansion is a lookup, not an opinion.** We ask the directory what contains
what. We never infer membership from naming conventions, however tempting
``x-team`` and ``x-team-leads`` look.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

#: How deep nesting may go before we stop and say so. Directories in the wild
#: rarely exceed four or five levels; anything past this is a modelling error.
MAX_DEPTH = 12


@dataclass
class GroupGraph:
    """Which groups contain which other groups.

    ``members[parent]`` holds the groups *directly* inside ``parent``. User
    membership is not modelled here: a principal arrives with its own direct
    groups already resolved by the identity layer, and this expands them upward.
    """

    members: dict[str, set[str]] = field(default_factory=dict)

    #: Set when an expansion hit a cycle or the depth bound. Surfaced so an
    #: operator can fix the directory rather than wonder about odd denials.
    warnings: list[str] = field(default_factory=list)

    def contains(self, parent: str, *children: str) -> GroupGraph:
        self.members.setdefault(parent, set()).update(children)
        return self

    def parents_of(self, group: str) -> set[str]:
        """Groups that directly contain this one."""
        return {parent for parent, children in self.members.items() if group in children}

    def expand(self, groups: Iterable[str]) -> frozenset[str]:
        """Every group a member of ``groups`` effectively belongs to.

        Walks *upward*: being in ``payments-team`` means being in
        ``engineering`` if engineering contains it, so a grant to engineering
        applies. Downward expansion would be the opposite error — treating a
        grant to a subgroup as a grant to its parent.
        """
        seen: set[str] = set()
        frontier = list(groups)
        depth = 0

        while frontier and depth < MAX_DEPTH:
            next_frontier: list[str] = []
            for group in frontier:
                if group in seen:
                    # The cycle guard. Not an error — real directories contain
                    # loops — just a branch already walked.
                    continue
                seen.add(group)
                next_frontier.extend(self.parents_of(group))
            frontier = next_frontier
            depth += 1

        if frontier:
            message = f"group nesting deeper than {MAX_DEPTH}; expansion stopped"
            self.warnings.append(message)
            log.warning("groups.depth_exceeded", max_depth=MAX_DEPTH, remaining=len(frontier))

        return frozenset(seen)

    def effective_groups(self, direct: Iterable[str]) -> frozenset[str]:
        return self.expand(direct)


#: A graph with no nesting. Every group is exactly itself, which is the correct
#: behaviour when a deployment has not described its directory to us — better
#: than guessing at hierarchy from names.
FLAT = GroupGraph()
