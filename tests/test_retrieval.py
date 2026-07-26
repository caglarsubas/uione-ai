"""Permission-aware retrieval.

The scenario every one of these guards against: an intern searches for
"restructuring" and the assistant returns the layoff plan, because the index knew
the document existed and nobody asked whether that person could see it.
"""

from __future__ import annotations

import pytest

from uione.knowledge import (
    AccessControl,
    Document,
    DocumentIndex,
    Visibility,
    tokenize,
)
from uione.mcphub import Principal, RiskClass

ALICE = Principal(user_id="alice", roles=frozenset({"finance", "employee"}))
INTERN = Principal(user_id="intern", roles=frozenset({"employee"}))
HR = Principal(user_id="hr-lead", roles=frozenset({"hr", "employee"}))


def doc(doc_id: str, title: str, body: str, acl: AccessControl, **kwargs) -> Document:
    return Document(id=doc_id, title=title, body=body, source="wiki.search", acl=acl, **kwargs)


@pytest.fixture
def index() -> DocumentIndex:
    index = DocumentIndex()
    index.add_all(
        [
            doc(
                "public-1",
                "Annual leave policy",
                "The carry-over limit changes from ten days to five days in September.",
                AccessControl.organisation_wide(),
            ),
            doc(
                "finance-1",
                "Q3 budget restructuring",
                "Departmental budget restructuring and headcount forecast for Q3.",
                AccessControl.for_groups("finance"),
            ),
            doc(
                "hr-1",
                "Restructuring plan — confidential",
                "Proposed redundancies and the restructuring timetable. Not for circulation.",
                AccessControl.for_groups("hr"),
            ),
            doc(
                "personal-1",
                "Alice performance review",
                "Performance review notes and restructuring impact on her team.",
                AccessControl.for_users("alice"),
            ),
        ]
    )
    return index


# -- the scenario this exists for ------------------------------------------


def test_an_intern_searching_restructuring_gets_nothing_confidential(index: DocumentIndex) -> None:
    """The career-ending failure, asserted directly."""
    hits = index.search(INTERN, "restructuring")

    assert [h.document.id for h in hits] == []


def test_finance_sees_only_the_finance_document(index: DocumentIndex) -> None:
    hits = index.search(ALICE, "restructuring")

    ids = {h.document.id for h in hits}
    assert "finance-1" in ids
    assert "hr-1" not in ids


def test_hr_sees_the_confidential_plan(index: DocumentIndex) -> None:
    assert "hr-1" in {h.document.id for h in index.search(HR, "restructuring")}


def test_organisation_wide_documents_reach_everyone(index: DocumentIndex) -> None:
    assert "public-1" in {h.document.id for h in index.search(INTERN, "leave policy")}


def test_a_personal_document_reaches_only_its_owner(index: DocumentIndex) -> None:
    assert "personal-1" in {h.document.id for h in index.search(ALICE, "performance review")}
    assert index.search(HR, "performance review") == []


# -- filtering happens before ranking --------------------------------------


def test_scores_do_not_shift_when_invisible_documents_are_added(index: DocumentIndex) -> None:
    """The inference channel that 'retrieve then filter' leaves open.

    If corpus statistics were global, adding a document the intern cannot see
    would move the intern's scores — revealing that something was added.
    """
    before = index.search(INTERN, "leave policy")[0].score

    for i in range(20):
        index.add(
            doc(
                f"secret-{i}",
                "Leave policy exception",
                "leave policy",
                AccessControl.for_groups("hr"),
            )
        )

    after = index.search(INTERN, "leave policy")[0].score

    assert before == after


def test_result_counts_do_not_reveal_hidden_documents(index: DocumentIndex) -> None:
    """'No results' and 'results you cannot see' must be indistinguishable."""
    assert index.search(INTERN, "restructuring") == []
    assert index.search(INTERN, "a topic nobody has written about") == []


def test_visible_count_is_per_principal(index: DocumentIndex) -> None:
    assert index.visible_count(INTERN) == 1
    assert index.visible_count(ALICE) == 3
    assert index.visible_count(HR) == 2


# -- deny by default -------------------------------------------------------


def test_a_document_with_no_acl_is_visible_to_nobody() -> None:
    """An ingestion bug that loses permissions must hide content, not publish it."""
    index = DocumentIndex()
    index.add(doc("orphan", "Lost permissions", "merger terms", AccessControl()))

    for principal in (ALICE, INTERN, HR):
        assert index.search(principal, "merger") == []


def test_unreadable_documents_are_counted_for_operators() -> None:
    """A whole source in this state means broken sync, not a private corpus."""
    index = DocumentIndex()
    index.add(doc("orphan", "Lost", "text", AccessControl()))
    index.add(doc("fine", "Fine", "text", AccessControl.organisation_wide()))

    assert index.stats().unreadable == 1


def test_the_default_access_control_is_restrictive() -> None:
    assert AccessControl().visibility is Visibility.RESTRICTED
    assert AccessControl().empty


# -- denials win -----------------------------------------------------------


def test_an_explicit_denial_beats_a_group_grant() -> None:
    """Source systems have deny rules; dropping them silently widens access."""
    index = DocumentIndex()
    index.add(
        doc(
            "d1",
            "Team plan",
            "restructuring",
            AccessControl(groups=frozenset({"finance"}), denied_users=frozenset({"alice"})),
        )
    )

    assert index.search(ALICE, "restructuring") == []


def test_a_denial_beats_organisation_wide() -> None:
    index = DocumentIndex()
    index.add(
        doc(
            "d1",
            "Notice",
            "restructuring",
            AccessControl(visibility=Visibility.ORGANISATION, denied_users=frozenset({"intern"})),
        )
    )

    assert index.search(INTERN, "restructuring") == []
    assert index.search(ALICE, "restructuring")


# -- revocation ------------------------------------------------------------


def test_revoking_access_takes_effect_immediately(index: DocumentIndex) -> None:
    """A permission removed at the source is a live leak until it lands here."""
    assert index.search(ALICE, "restructuring")

    index.update_acl("finance-1", AccessControl.for_groups("hr"))

    assert "finance-1" not in {h.document.id for h in index.search(ALICE, "restructuring")}


def test_granting_access_takes_effect_immediately(index: DocumentIndex) -> None:
    assert index.search(INTERN, "restructuring") == []

    index.update_acl("hr-1", AccessControl.organisation_wide())

    assert "hr-1" in {h.document.id for h in index.search(INTERN, "restructuring")}


def test_updating_an_unknown_document_reports_failure(index: DocumentIndex) -> None:
    assert index.update_acl("nope", AccessControl.organisation_wide()) is False


def test_a_whole_source_can_be_dropped(index: DocumentIndex) -> None:
    """The right response to not knowing who may see a source's content."""
    removed = index.remove_source("wiki.search")

    assert removed == 4
    assert len(index) == 0


# -- direct fetch ----------------------------------------------------------


def test_fetching_by_id_respects_permissions(index: DocumentIndex) -> None:
    assert index.get(HR, "hr-1") is not None
    assert index.get(INTERN, "hr-1") is None


def test_missing_and_forbidden_are_indistinguishable(index: DocumentIndex) -> None:
    """Otherwise probing ids reveals which documents exist."""
    assert index.get(INTERN, "hr-1") is None
    assert index.get(INTERN, "does-not-exist") is None


# -- ranking ---------------------------------------------------------------


def test_better_matches_rank_higher() -> None:
    index = DocumentIndex()
    index.add(
        doc(
            "a",
            "Payment gateway latency",
            "latency latency latency gateway",
            AccessControl.organisation_wide(),
        )
    )
    index.add(
        doc("b", "Weekly notes", "we discussed latency once", AccessControl.organisation_wide())
    )

    hits = index.search(ALICE, "latency")

    assert [h.document.id for h in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score


def test_the_limit_is_respected() -> None:
    index = DocumentIndex()
    for i in range(20):
        index.add(doc(f"d{i}", f"Latency note {i}", "latency", AccessControl.organisation_wide()))

    assert len(index.search(ALICE, "latency", limit=3)) == 3


def test_an_empty_query_returns_nothing(index: DocumentIndex) -> None:
    assert index.search(ALICE, "") == []
    assert index.search(ALICE, "the and of") == []


def test_snippets_centre_on_the_query(index: DocumentIndex) -> None:
    hit = index.search(HR, "redundancies")[0]

    assert "redundancies" in hit.snippet.lower()


# -- reindexing ------------------------------------------------------------


def test_readding_a_document_replaces_it(index: DocumentIndex) -> None:
    index.add(
        doc(
            "public-1",
            "Annual leave policy",
            "completely different text",
            AccessControl.organisation_wide(),
        )
    )

    assert len(index) == 4
    assert index.search(ALICE, "carry-over") == []


def test_removing_a_document_removes_its_terms(index: DocumentIndex) -> None:
    index.remove("hr-1")

    assert index.search(HR, "redundancies") == []


def test_acl_fingerprints_detect_drift() -> None:
    """Cheap comparison against the source, without refetching the content."""
    first = AccessControl.for_groups("finance")
    same = AccessControl.for_groups("finance")
    different = AccessControl.for_groups("finance", "hr")

    assert first.fingerprint() == same.fingerprint()
    assert first.fingerprint() != different.fingerprint()


# -- tokenisation ----------------------------------------------------------


def test_stopwords_are_dropped_but_short_real_words_survive() -> None:
    """An aggressive stopword list breaks searches for real things."""
    tokens = tokenize("the IT and AI budget for the quarter")

    assert "the" not in tokens
    assert "it" in tokens
    assert "ai" in tokens


def test_tokenisation_is_case_insensitive() -> None:
    assert tokenize("Restructuring") == tokenize("RESTRUCTURING")


# -- through the gateway ---------------------------------------------------


async def build_gateway(index: DocumentIndex):
    from uione.knowledge import build_knowledge_source
    from uione.mcphub import AuditLog, Grant, InMemoryAuditSink, McpGateway, ToolPolicy

    sink = InMemoryAuditSink()
    gateway = McpGateway(
        policy=ToolPolicy(
            [
                Grant(role="employee", tools=frozenset({"knowledge.*"}), max_risk=RiskClass.READ),
                Grant(role="finance", tools=frozenset({"knowledge.*"}), max_risk=RiskClass.READ),
                Grant(role="hr", tools=frozenset({"knowledge.*"}), max_risk=RiskClass.READ),
            ]
        ),
        audit=AuditLog(sink),
    )
    await gateway.register(build_knowledge_source(index))
    return gateway, sink


async def test_the_leak_scenario_end_to_end(index: DocumentIndex) -> None:
    """Through the gateway, as a real request would arrive."""
    gateway, _ = await build_gateway(index)

    intern = await gateway.call(INTERN, "knowledge.search", {"query": "restructuring"})
    hr = await gateway.call(HR, "knowledge.search", {"query": "restructuring"})

    assert intern.ok
    assert "confidential" not in intern.result.content.lower()
    assert intern.result.structured["count"] == 0
    assert "hr-1" in hr.result.content


async def test_empty_and_forbidden_read_identically(index: DocumentIndex) -> None:
    """A user must not learn that documents exist by the shape of the answer."""
    gateway, _ = await build_gateway(index)

    forbidden = await gateway.call(INTERN, "knowledge.search", {"query": "restructuring"})
    nonexistent = await gateway.call(INTERN, "knowledge.search", {"query": "zeppelins"})

    assert forbidden.result.structured == nonexistent.result.structured


async def test_fetching_a_forbidden_document_through_the_gateway(index: DocumentIndex) -> None:
    gateway, _ = await build_gateway(index)

    call = await gateway.call(INTERN, "knowledge.fetch", {"id": "hr-1"})

    assert not call.ok
    assert "confidential" not in (call.result.error or "").lower()


async def test_each_caller_is_filtered_independently(index: DocumentIndex) -> None:
    """The same tool, two callers, two different corpora — no cached bleed."""
    gateway, _ = await build_gateway(index)

    first = await gateway.call(HR, "knowledge.search", {"query": "restructuring"})
    second = await gateway.call(INTERN, "knowledge.search", {"query": "restructuring"})
    third = await gateway.call(HR, "knowledge.search", {"query": "restructuring"})

    assert first.result.content == third.result.content
    assert second.result.structured["count"] == 0


async def test_searches_are_audited(index: DocumentIndex) -> None:
    """Reads can leak, so they are recorded like anything else."""
    gateway, sink = await build_gateway(index)

    await gateway.call(HR, "knowledge.search", {"query": "redundancies"})

    assert sink.records[0].tool == "knowledge.search"
    assert sink.records[0].principal_id == "hr-lead"


async def test_documents_are_marked_untrusted(index: DocumentIndex) -> None:
    """Indexed files are written by people, including outside parties."""
    gateway, _ = await build_gateway(index)

    assert gateway.spec("knowledge.search").returns_untrusted_content
