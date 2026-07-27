"""Inherited permissions and nested groups.

POSIX is contested but shallow. This is the deeper shape — Confluence spaces and
page restrictions, SharePoint sites with broken inheritance, nested LDAP groups —
where the failures are subtle and each one either denies someone who should have
access or, worse, grants someone who should not.
"""

from __future__ import annotations

import pytest

from uione.knowledge import (
    MAX_DEPTH,
    AccessControl,
    AclNode,
    Document,
    DocumentIndex,
    GroupGraph,
    Hierarchy,
    Visibility,
    principal_groups,
)
from uione.mcphub import Principal


def principal(user_id: str, *roles: str) -> Principal:
    return Principal(user_id=user_id, roles=frozenset(roles))


ALICE = principal("alice", "payments-team")
BOB = principal("bob", "engineering")
OUTSIDER = principal("outsider", "sales")
CONTRACTOR = principal("contractor", "payments-team")


# -- nested groups ---------------------------------------------------------


@pytest.fixture
def graph() -> GroupGraph:
    return (
        GroupGraph()
        .contains("engineering", "payments-team", "platform-team")
        .contains("all-staff", "engineering", "sales")
    )


def test_membership_reaches_containing_groups(graph: GroupGraph) -> None:
    """Being in payments-team means a grant to engineering applies."""
    effective = graph.effective_groups({"payments-team"})

    assert "payments-team" in effective
    assert "engineering" in effective
    assert "all-staff" in effective


def test_expansion_goes_upward_not_downward(graph: GroupGraph) -> None:
    """Being in engineering does not put you in payments-team."""
    effective = graph.effective_groups({"engineering"})

    assert "payments-team" not in effective
    assert "all-staff" in effective


def test_an_unknown_group_expands_to_itself(graph: GroupGraph) -> None:
    assert graph.effective_groups({"mystery"}) == frozenset({"mystery"})


def test_a_cycle_terminates() -> None:
    """Real directories contain loops, usually by accident."""
    cyclic = GroupGraph().contains("a", "b").contains("b", "c").contains("c", "a")

    effective = cyclic.effective_groups({"a"})

    assert effective == frozenset({"a", "b", "c"})


def test_a_self_containing_group_terminates() -> None:
    assert GroupGraph().contains("a", "a").effective_groups({"a"}) == frozenset({"a"})


def test_deep_nesting_is_bounded_and_reported() -> None:
    """A silently truncated expansion removes access and looks like a bug."""
    deep = GroupGraph()
    for i in range(MAX_DEPTH + 5):
        deep.contains(f"g{i + 1}", f"g{i}")

    deep.effective_groups({"g0"})

    assert deep.warnings, "exceeding the bound must be surfaced, not silent"


def test_a_flat_graph_changes_nothing() -> None:
    """The correct behaviour when a deployment has not described its directory."""
    assert GroupGraph().effective_groups({"a", "b"}) == frozenset({"a", "b"})


def test_principal_groups_helper(graph: GroupGraph) -> None:
    assert "engineering" in principal_groups(frozenset({"payments-team"}), graph)


# -- hierarchy: the common case -------------------------------------------


@pytest.fixture
def space() -> Hierarchy:
    """A Confluence-shaped space: space → page → child page."""
    hierarchy = Hierarchy()
    hierarchy.add_all(
        [
            AclNode(id="space", groups=frozenset({"engineering"}), label="Engineering space"),
            AclNode(id="page", parent_id="space", label="Payments runbook"),
            AclNode(id="child", parent_id="page", label="Escalation notes"),
        ]
    )
    return hierarchy


def test_a_child_inherits_its_ancestors_grants(space: Hierarchy) -> None:
    acl = space.resolve("child")

    assert acl.groups == frozenset({"engineering"})


def test_a_node_adds_its_own_grants_to_inherited_ones(space: Hierarchy) -> None:
    space.add(AclNode(id="page", parent_id="space", users=frozenset({"auditor"})))

    acl = space.resolve("page")

    assert acl.groups == frozenset({"engineering"})
    assert acl.users == frozenset({"auditor"})


def test_an_unknown_node_grants_nothing(space: Hierarchy) -> None:
    """An unknown node is not a permissive one."""
    assert space.resolve("nonexistent").empty


def test_organisation_wide_inherits_downward(space: Hierarchy) -> None:
    space.add(AclNode(id="space", organisation_wide=True))

    assert space.resolve("child").visibility is Visibility.ORGANISATION


# -- breaking inheritance --------------------------------------------------


def test_breaking_inheritance_drops_ancestor_grants(space: Hierarchy) -> None:
    """A restricted page stops taking the space's permissions."""
    space.add(AclNode(id="page", parent_id="space", inherits=False, users=frozenset({"lead"})))

    acl = space.resolve("page")

    assert acl.users == frozenset({"lead"})
    assert acl.groups == frozenset(), "the space's grant must not survive a break"


def test_a_break_applies_to_everything_below_it(space: Hierarchy) -> None:
    space.add(AclNode(id="page", parent_id="space", inherits=False, users=frozenset({"lead"})))

    acl = space.resolve("child")

    assert acl.users == frozenset({"lead"})
    assert acl.groups == frozenset()


def test_a_break_still_collects_grants_below_itself(space: Hierarchy) -> None:
    space.add(AclNode(id="page", parent_id="space", inherits=False, users=frozenset({"lead"})))
    space.add(AclNode(id="child", parent_id="page", users=frozenset({"reviewer"})))

    acl = space.resolve("child")

    assert acl.users == frozenset({"lead", "reviewer"})


# -- the asymmetry that matters -------------------------------------------


def test_denials_survive_a_break(space: Hierarchy) -> None:
    """The bug this prevents: a break readmitting a departed contractor.

    A break is a statement about who *may* read, not a pardon.
    """
    space.add(
        AclNode(
            id="space",
            groups=frozenset({"engineering"}),
            denied_users=frozenset({"contractor"}),
        )
    )
    space.add(
        AclNode(
            id="page",
            parent_id="space",
            inherits=False,
            groups=frozenset({"payments-team"}),
        )
    )

    acl = space.resolve("page")

    assert "contractor" in acl.denied_users
    assert not acl.permits(CONTRACTOR)


def test_a_denial_at_any_depth_reaches_the_leaf(space: Hierarchy) -> None:
    space.add(
        AclNode(
            id="page",
            parent_id="space",
            denied_users=frozenset({"contractor"}),
        )
    )

    assert not space.resolve("child").permits(CONTRACTOR)


def test_a_denial_beats_an_organisation_wide_grant(space: Hierarchy) -> None:
    space.add(AclNode(id="space", organisation_wide=True, denied_users=frozenset({"outsider"})))

    acl = space.resolve("child")

    assert acl.permits(BOB)
    assert not acl.permits(OUTSIDER)


# -- malformed hierarchies -------------------------------------------------


def test_a_parent_cycle_terminates() -> None:
    """A soft-deleted container whose parent pointer still resolves."""
    hierarchy = Hierarchy()
    hierarchy.add_all(
        [
            AclNode(id="a", parent_id="b", users=frozenset({"alice"})),
            AclNode(id="b", parent_id="a"),
        ]
    )

    acl = hierarchy.resolve("a")

    assert acl.users == frozenset({"alice"})


def test_a_missing_parent_stops_the_walk() -> None:
    hierarchy = Hierarchy()
    hierarchy.add(AclNode(id="orphan", parent_id="deleted", users=frozenset({"alice"})))

    assert hierarchy.resolve("orphan").users == frozenset({"alice"})


def test_a_very_deep_chain_is_bounded() -> None:
    hierarchy = Hierarchy()
    hierarchy.add(AclNode(id="n0", users=frozenset({"root-user"})))
    for i in range(1, 100):
        hierarchy.add(AclNode(id=f"n{i}", parent_id=f"n{i - 1}"))

    acl = hierarchy.resolve("n99")

    # Bounded, so the root's grant is not reached — stricter, not looser.
    assert "root-user" not in acl.users


# -- end to end through the index -----------------------------------------


def document(doc_id: str, body: str, acl: AccessControl) -> Document:
    return Document(id=doc_id, title=doc_id, body=body, source="wiki", acl=acl)


def test_a_grant_to_a_parent_group_reaches_a_subgroup_member(graph: GroupGraph) -> None:
    """The whole point of nesting: alice is only in payments-team."""
    index = DocumentIndex(groups=graph)
    index.add(document("d1", "the payments runbook", AccessControl.for_groups("engineering")))

    assert index.search(ALICE, "runbook")
    assert index.search(OUTSIDER, "runbook") == []


def test_without_a_group_graph_nesting_is_not_assumed() -> None:
    """A deployment that has not described its directory gets literal matching."""
    index = DocumentIndex()
    index.add(document("d1", "the payments runbook", AccessControl.for_groups("engineering")))

    assert index.search(ALICE, "runbook") == []
    assert index.search(BOB, "runbook")


def test_a_resolved_hierarchy_feeds_the_index(graph: GroupGraph) -> None:
    hierarchy = Hierarchy()
    hierarchy.add_all(
        [
            AclNode(id="space", groups=frozenset({"engineering"})),
            AclNode(
                id="restricted",
                parent_id="space",
                inherits=False,
                users=frozenset({"lead"}),
                denied_users=frozenset({"contractor"}),
            ),
        ]
    )
    index = DocumentIndex(groups=graph)
    index.add(document("open", "the general runbook", hierarchy.resolve("space")))
    index.add(document("secret", "the escalation runbook", hierarchy.resolve("restricted")))

    assert {h.document.id for h in index.search(ALICE, "runbook")} == {"open"}
    assert {h.document.id for h in index.search(principal("lead"), "runbook")} == {"secret"}


def test_a_denied_contractor_sees_nothing_even_with_the_right_group(
    graph: GroupGraph,
) -> None:
    hierarchy = Hierarchy()
    hierarchy.add(
        AclNode(
            id="space",
            groups=frozenset({"engineering"}),
            denied_users=frozenset({"contractor"}),
        )
    )
    index = DocumentIndex(groups=graph)
    index.add(document("d1", "the payments runbook", hierarchy.resolve("space")))

    assert index.search(ALICE, "runbook")
    assert index.search(CONTRACTOR, "runbook") == []


def test_expansion_does_not_change_stored_acls(graph: GroupGraph) -> None:
    """Operators compare our ACL against the source; it must say what they said."""
    index = DocumentIndex(groups=graph)
    index.add(document("d1", "text", AccessControl.for_groups("engineering")))

    assert index.acl_of("d1").groups == frozenset({"engineering"})
