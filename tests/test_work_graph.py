from __future__ import annotations

import pytest

from uione.knowledge import (
    EntityKind,
    ExtractionRules,
    GraphItem,
    WorkGraph,
    entity,
    extract_entities,
)

RULES = ExtractionRules(
    ticket_prefixes=frozenset({"PAY", "OPS"}),
    incident_prefixes=frozenset({"INC"}),
    reference_prefixes=frozenset({"INV"}),
    internal_domains=frozenset({"corp.example"}),
)


def item(source: str, kind: EntityKind, key: str, title: str = "", body: str = "") -> GraphItem:
    return GraphItem(source=source, subject=entity(kind, key), title=title, body=body)


# -- extraction ------------------------------------------------------------


def test_ticket_keys_are_extracted() -> None:
    found = extract_entities("Please look at PAY-1182 today", RULES)
    assert entity(EntityKind.TICKET, "PAY-1182") in found


def test_incident_prefix_classifies_as_incident_not_ticket() -> None:
    found = extract_entities("INC-4471 is still open", RULES)
    assert entity(EntityKind.INCIDENT, "INC-4471") in found
    assert entity(EntityKind.TICKET, "INC-4471") not in found


def test_business_references_are_extracted() -> None:
    """These are the highest-value links: no single system owns them."""
    found = extract_entities("discrepancy on INV-88213", RULES)
    assert entity(EntityKind.REFERENCE, "INV-88213") in found


def test_case_is_normalised_so_systems_agree() -> None:
    """A mailbox writes pay-1182, a tracker writes PAY-1182; they must collapse."""
    assert extract_entities("pay-1182", RULES) == extract_entities("PAY-1182", RULES)


def test_unknown_prefixes_are_ignored_by_default() -> None:
    """COVID-19 and ISO-9001 are not tickets, and a wrong link is worse than none."""
    found = extract_entities("Per ISO-9001 and COVID-19 guidance", RULES)
    assert found == set()


def test_unknown_prefixes_can_be_opted_into() -> None:
    permissive = ExtractionRules(allow_unknown_prefixes=True, extract_people=False)
    assert extract_entities("ABC-123", permissive)


def test_people_are_extracted_from_addresses() -> None:
    found = extract_entities("ask cfo@corp.example", RULES)
    assert entity(EntityKind.PERSON, "cfo@corp.example") in found


def test_message_ids_are_not_mistaken_for_people() -> None:
    """Threading metadata is not a colleague; adding it pollutes person queries."""
    found = extract_entities("In-Reply-To: <abc123@mail.corp.example>", RULES)
    assert not any(e.kind is EntityKind.PERSON for e in found)


def test_jira_urls_yield_the_key() -> None:
    found = extract_entities("https://jira.corp.example/browse/PAY-1182", RULES)
    assert entity(EntityKind.TICKET, "PAY-1182") in found


def test_empty_text_extracts_nothing() -> None:
    assert extract_entities("", RULES) == set()


# -- linking ---------------------------------------------------------------


@pytest.fixture
def graph() -> WorkGraph:
    """The scenario the strategy doc uses: mail, ticket and incident, one story."""
    graph = WorkGraph(RULES)
    graph.add_all(
        [
            item(
                "mail.list_unread",
                EntityKind.MESSAGE,
                "m-3",
                title="Re: invoice reconciliation",
                body="We show a 4,200 EUR discrepancy on INV-88213.",
            ),
            item(
                "tasks.my_open_issues",
                EntityKind.TICKET,
                "PAY-1182",
                title="Reconcile supplier invoice INV-88213",
            ),
            item(
                "mail.list_unread",
                EntityKind.MESSAGE,
                "m-1",
                title="P1: payment gateway latency",
                body="Incident INC-4471 opened.",
            ),
            item(
                "incidents.active",
                EntityKind.INCIDENT,
                "INC-4471",
                title="Payment gateway p99 latency breach",
            ),
            item(
                "calendar.today",
                EntityKind.MEETING,
                "cal-1",
                title="Incident review — INC-4471",
            ),
        ]
    )
    return graph


def test_mail_and_ticket_link_through_an_invoice_number(graph: WorkGraph) -> None:
    """The join the model previously had to notice by luck."""
    message = next(i for i in graph.items if i.subject.key == "m-3")

    links = graph.links_for(message)

    targets = {link.target.subject.key for link in links}
    assert "PAY-1182" in targets


def test_a_link_can_be_explained(graph: WorkGraph) -> None:
    """'Related, trust me' is what erodes confidence when it is occasionally wrong."""
    message = next(i for i in graph.items if i.subject.key == "m-3")

    link = next(link for link in graph.links_for(message) if link.target.subject.key == "PAY-1182")

    assert "INV-88213" in link.explain()


def test_about_returns_everything_touching_an_entity(graph: WorkGraph) -> None:
    items = graph.about(entity(EntityKind.INCIDENT, "INC-4471"))

    assert {i.subject.key for i in items} == {"INC-4471", "m-1", "cal-1"}


def test_alert_mail_is_a_duplicate_of_the_incident(graph: WorkGraph) -> None:
    """One event through two channels should appear once in a brief (gap G7)."""
    incident = next(i for i in graph.items if i.subject.key == "INC-4471")

    duplicates = graph.duplicates_of(incident)

    assert {d.subject.key for d in duplicates} == {"m-1", "cal-1"}


def test_sharing_a_reference_does_not_make_items_duplicates(graph: WorkGraph) -> None:
    """Over-merging hides things; related is not the same as identical."""
    message = next(i for i in graph.items if i.subject.key == "m-3")

    assert graph.duplicates_of(message) == []


def test_an_item_is_never_its_own_duplicate(graph: WorkGraph) -> None:
    incident = next(i for i in graph.items if i.subject.key == "INC-4471")
    assert incident not in graph.duplicates_of(incident)


# -- clustering ------------------------------------------------------------


def test_clusters_group_a_piece_of_work(graph: WorkGraph) -> None:
    clusters = {c.anchor.key: c for c in graph.clusters()}

    assert "INC-4471" in clusters
    assert len(clusters["INC-4471"].items) == 3


def test_cross_system_clusters_are_identified(graph: WorkGraph) -> None:
    """The joins the user cannot make by glancing at one tool."""
    anchors = {c.anchor.key for c in graph.cross_system_clusters()}

    assert "INC-4471" in anchors
    assert "INV-88213" in anchors


def test_a_cluster_inside_one_system_is_not_cross_system() -> None:
    graph = WorkGraph(RULES)
    graph.add_all(
        [
            item("tasks.my_open_issues", EntityKind.TICKET, "PAY-1", body="see PAY-9"),
            item("tasks.my_open_issues", EntityKind.TICKET, "PAY-9", body="see PAY-1"),
        ]
    )

    assert graph.cross_system_clusters() == []


def test_singleton_entities_do_not_form_clusters(graph: WorkGraph) -> None:
    assert all(len(c.items) >= 2 for c in graph.clusters())


def test_render_context_describes_the_connections(graph: WorkGraph) -> None:
    rendered = graph.render_context()

    assert "connected across systems" in rendered
    assert "INC-4471" in rendered


def test_render_context_is_empty_without_cross_system_links() -> None:
    """No spurious 'connections' section when there is nothing to say."""
    graph = WorkGraph(RULES)
    graph.add(item("tasks.my_open_issues", EntityKind.TICKET, "PAY-1", title="Lonely"))

    assert graph.render_context() == ""


def test_ingesting_the_same_item_twice_is_idempotent(graph: WorkGraph) -> None:
    before = len(graph.items)

    graph.add(item("incidents.active", EntityKind.INCIDENT, "INC-4471", title="Same"))

    assert len(graph.items) == before


# -- ServiceNow record numbers ----------------------------------------------
#
# These carry no separator, so the hyphenated pattern cannot see them. Before
# this existed, connecting the ServiceNow connector meant incidents linked to
# nothing: an email naming INC0010001 and the incident itself were unrelated
# records as far as the work graph was concerned — the product's differentiating
# feature failing silently on its most likely enterprise source.


def test_a_servicenow_incident_number_is_an_identifier() -> None:
    found = extract_entities("Incident INC0010001 opened at 07:35", RULES)

    assert entity(EntityKind.INCIDENT, "INC0010001") in found


def test_a_change_record_is_a_ticket_not_an_incident() -> None:
    """The table the number comes from says what kind of thing it is."""
    found = extract_entities("CHG0030004 is scheduled for Sunday", RULES)

    assert entity(EntityKind.TICKET, "CHG0030004") in found
    assert entity(EntityKind.INCIDENT, "CHG0030004") not in found


def test_it_works_without_the_prefix_being_declared() -> None:
    """A deployment that connected ServiceNow but never added "INC" to its
    prefix list would otherwise find that incidents link to nothing."""
    rules = ExtractionRules(ticket_prefixes=frozenset({"PAY"}))

    found = extract_entities("see INC0010001", rules)

    assert entity(EntityKind.INCIDENT, "INC0010001") in found


def test_it_can_be_switched_off() -> None:
    """ "Declare nothing, extract nothing" stays available, because every field
    in this module is configuration."""
    rules = ExtractionRules(ticket_prefixes=frozenset(), extract_servicenow=False)

    assert extract_entities("see INC0010001", rules) == set()


@pytest.mark.parametrize("text", ["COVID-19 update", "UTF-8 encoding", "INC001", "INC00100012"])
def test_prose_is_not_mistaken_for_a_record_number(text: str) -> None:
    """Seven digits exactly, no separator. The shape is the whole filter, so it
    has to be a tight one."""
    found = extract_entities(text, ExtractionRules(ticket_prefixes=frozenset()))

    assert found == set()


def test_a_lowercase_reference_still_matches() -> None:
    """People write "inc0010001" in email, and dropping it loses exactly the
    cross-system link this module exists to find."""
    found = extract_entities("looking at inc0010001 now", RULES)

    assert entity(EntityKind.INCIDENT, "INC0010001") in found
