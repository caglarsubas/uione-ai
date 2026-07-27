"""Inherited permissions over a content hierarchy.

The shared shape of Confluence (space → page → child page), SharePoint (site →
library → folder → item) and shared drives: a node inherits its parent's
permissions until someone *breaks* inheritance and states its own.

This module is the resolution algorithm, not a vendor connector. It is written
against semantics stated below rather than against any product's API, and the
mapping from a real system's model onto these three fields is where the next
argument will be — so the semantics are spelled out precisely enough to be
checked against a vendor's documentation.

**The semantics:**

1. A node with `inherits=True` takes the resolved permissions of its parent and
   *adds* its own grants. This is the common case and the one people reason about.
2. A node with `inherits=False` uses only its own grants. Its ancestors become
   irrelevant for allowing — SharePoint calls this breaking inheritance,
   Confluence calls it a page restriction.
3. **Denials always inherit, even through a break.** This is the asymmetry that
   matters. A break is a statement about who *may* read, not a pardon: if an
   ancestor explicitly excluded someone, a subtree cannot quietly readmit them.
   Treating a break as clearing denials is the bug that reinstates a departed
   contractor.

Rule 3 is a deliberate choice and stricter than some products. Where a real
system disagrees, the connector should record that rather than this module
loosening — being stricter than the source denies access someone should have,
which they report; being looser grants access nobody asked for, which they do not.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from uione.knowledge.documents import AccessControl, Visibility
from uione.knowledge.groups import FLAT, GroupGraph

log = structlog.get_logger(__name__)

#: Guard against a malformed hierarchy — a node whose parent chain loops, which
#: an import from a system with soft-deleted containers can produce.
MAX_ANCESTRY = 32


@dataclass
class AclNode:
    """One node in a permission hierarchy."""

    id: str
    parent_id: str | None = None

    #: Whether this node's permissions extend its parent's.
    inherits: bool = True

    users: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    denied_users: frozenset[str] = frozenset()

    #: Everyone in the organisation may read this node.
    organisation_wide: bool = False

    label: str = ""

    @property
    def grants_anything(self) -> bool:
        return bool(self.users or self.groups or self.organisation_wide)


class Hierarchy:
    """A tree of permission nodes, resolvable to flat ACLs."""

    def __init__(self, *, groups: GroupGraph | None = None) -> None:
        self._nodes: dict[str, AclNode] = {}
        self._groups = groups or FLAT

    def add(self, node: AclNode) -> AclNode:
        self._nodes[node.id] = node
        return node

    def add_all(self, nodes: list[AclNode]) -> None:
        for node in nodes:
            self.add(node)

    def get(self, node_id: str) -> AclNode | None:
        return self._nodes.get(node_id)

    def ancestry(self, node_id: str) -> list[AclNode]:
        """The node and its ancestors, nearest first.

        Stops at a cycle rather than looping — a soft-deleted container whose
        parent pointer still resolves can produce one on import.
        """
        chain: list[AclNode] = []
        seen: set[str] = set()
        current = self._nodes.get(node_id)

        while current is not None and len(chain) < MAX_ANCESTRY:
            if current.id in seen:
                log.warning("hierarchy.cycle", node=current.id)
                break
            seen.add(current.id)
            chain.append(current)
            current = self._nodes.get(current.parent_id) if current.parent_id else None

        return chain

    def resolve(self, node_id: str) -> AccessControl:
        """Flatten a node's effective permissions.

        Walks from the node upward, accumulating grants until a break stops the
        walk — but continuing to collect *denials* the whole way to the root.
        """
        chain = self.ancestry(node_id)
        if not chain:
            # An unknown node is not a permissive one.
            return AccessControl()

        users: set[str] = set()
        groups: set[str] = set()
        denied: set[str] = set()
        organisation_wide = False
        collecting_grants = True

        for node in chain:
            if collecting_grants:
                users |= node.users
                groups |= node.groups
                organisation_wide = organisation_wide or node.organisation_wide

            # Denials keep accumulating past a break, deliberately. A break says
            # who may read; it does not pardon an explicit exclusion above.
            denied |= node.denied_users

            if not node.inherits:
                collecting_grants = False

        # Expand group nesting last, so a grant to a parent group reaches
        # everyone inside it however the grant was inherited.
        expanded = self._expanded_groups(groups)

        if organisation_wide:
            return AccessControl(visibility=Visibility.ORGANISATION, denied_users=frozenset(denied))

        return AccessControl(
            users=frozenset(users),
            groups=expanded,
            denied_users=frozenset(denied),
        )

    def _expanded_groups(self, groups: set[str]) -> frozenset[str]:
        """Include every group that contains one of these.

        A principal carrying ``payments-team`` should match a grant to
        ``engineering``. Expanding the *grant* downward would be wrong; instead
        the principal's own groups are expanded upward at check time, and this
        keeps the grant set as stated. Kept as a seam so a deployment that
        prefers grant-side expansion can choose it explicitly.
        """
        return frozenset(groups)

    def resolve_all(self) -> dict[str, AccessControl]:
        return {node_id: self.resolve(node_id) for node_id in self._nodes}

    def __len__(self) -> int:
        return len(self._nodes)


def principal_groups(direct: frozenset[str], graph: GroupGraph) -> frozenset[str]:
    """Every group a principal effectively belongs to.

    Expansion happens on the *principal* side rather than the grant side. Both
    can be made to work, but expanding the principal keeps stored ACLs identical
    to what the source system stated — so an operator comparing our ACL against
    Confluence sees the same names, and a nesting change takes effect without
    reindexing anything.
    """
    return graph.effective_groups(direct)
