"""Ingestion tests.

The index enforces permissions; ingestion decides what they are. These cover the
second half — including the bug this code shipped with in its first draft, where
a source with static permissions deleted its own entire content on every refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uione.connectors.mail import InMemoryMailBackend, MailMessage
from uione.knowledge import (
    AccessControl,
    CallableSource,
    Document,
    DocumentIndex,
    Ingestor,
    build_mail_ingestion,
)
from uione.mcphub import Principal

ALICE = Principal(user_id="alice", roles=frozenset({"employee"}))
BOB = Principal(user_id="bob", roles=frozenset({"employee"}))


def doc(doc_id: str, body: str, acl: AccessControl, source: str = "wiki") -> Document:
    return Document(id=doc_id, title=doc_id, body=body, source=source, acl=acl)


def static_source(name: str, documents: list[Document]) -> CallableSource:
    async def fetch(_since):
        return list(documents)

    return CallableSource(name=name, fetcher=fetch)


@pytest.fixture
def index() -> DocumentIndex:
    return DocumentIndex()


@pytest.fixture
def ingestor(index: DocumentIndex) -> Ingestor:
    return Ingestor(index)


# -- basic sync ------------------------------------------------------------


async def test_documents_reach_the_index(ingestor: Ingestor, index: DocumentIndex) -> None:
    ingestor.register(
        static_source("wiki", [doc("a", "budget notes", AccessControl.organisation_wide())])
    )

    result = await ingestor.sync("wiki")

    assert result.indexed == 1
    assert index.search(ALICE, "budget")


async def test_an_unknown_source_fails_clearly(ingestor: Ingestor) -> None:
    result = await ingestor.sync("nope")

    assert result.failed
    assert "unknown source" in result.error


async def test_a_failing_source_does_not_raise(ingestor: Ingestor) -> None:
    """One dead source is not an outage for the others."""

    async def explode(_since):
        raise ConnectionError("wiki unreachable")

    ingestor.register(CallableSource(name="wiki", fetcher=explode))

    result = await ingestor.sync("wiki")

    assert result.failed
    assert "ConnectionError" in result.error


async def test_one_failing_source_does_not_stop_the_others(
    ingestor: Ingestor, index: DocumentIndex
) -> None:
    async def explode(_since):
        raise ConnectionError("down")

    ingestor.register(CallableSource(name="broken", fetcher=explode))
    ingestor.register(
        static_source("wiki", [doc("a", "budget", AccessControl.organisation_wide())])
    )

    results = await ingestor.sync_all()

    assert {r.source: r.ok for r in results} == {"broken": False, "wiki": True}
    assert len(index) == 1


# -- the ACL rule ----------------------------------------------------------


async def test_a_document_without_an_acl_is_not_indexed(
    ingestor: Ingestor, index: DocumentIndex
) -> None:
    """'Index it and fix permissions later' — later does not arrive."""
    ingestor.register(
        static_source(
            "wiki",
            [
                doc("ok", "public text", AccessControl.organisation_wide()),
                doc("orphan", "merger terms", AccessControl()),
            ],
        )
    )

    result = await ingestor.sync("wiki")

    assert result.indexed == 1
    assert result.skipped_no_acl == 1
    assert len(index) == 1


async def test_skipped_documents_are_reported_not_silent(ingestor: Ingestor) -> None:
    ingestor.register(static_source("wiki", [doc("orphan", "text", AccessControl())]))

    result = await ingestor.sync("wiki")

    assert "skipped (no ACL)" in result.summary()


# -- permission re-sync ----------------------------------------------------


async def test_a_revoked_permission_is_applied(ingestor: Ingestor, index: DocumentIndex) -> None:
    """A permission removed at the source is a live leak until it lands here."""
    ingestor.register(
        CallableSource(
            name="wiki",
            fetcher=lambda _s: _documents([doc("a", "budget", AccessControl.for_users("alice"))]),
            acl_reader=lambda _ids: _acls({"a": AccessControl.for_users("bob")}),
        )
    )
    await ingestor.sync("wiki")
    assert index.search(ALICE, "budget")

    result = await ingestor.resync_permissions("wiki")

    assert result.revoked == 1
    assert index.search(ALICE, "budget") == []
    assert index.search(BOB, "budget")


async def test_a_document_the_source_forgot_is_removed(
    ingestor: Ingestor, index: DocumentIndex
) -> None:
    """If the source will not say who may read it, we no longer know."""
    ingestor.register(
        CallableSource(
            name="wiki",
            fetcher=lambda _s: _documents([doc("a", "budget", AccessControl.organisation_wide())]),
            acl_reader=lambda _ids: _acls({}),
        )
    )
    await ingestor.sync("wiki")

    result = await ingestor.resync_permissions("wiki")

    assert result.removed == 1
    assert len(index) == 0


async def test_unchanged_permissions_are_not_rewritten(ingestor: Ingestor) -> None:
    acl = AccessControl.for_groups("finance")
    ingestor.register(
        CallableSource(
            name="wiki",
            fetcher=lambda _s: _documents([doc("a", "budget", acl)]),
            acl_reader=lambda _ids: _acls({"a": AccessControl.for_groups("finance")}),
        )
    )
    await ingestor.sync("wiki")

    result = await ingestor.resync_permissions("wiki")

    assert result.revoked == 0


async def test_a_static_source_is_not_wiped_by_resync(
    ingestor: Ingestor, index: DocumentIndex
) -> None:
    """The bug this shipped with in its first draft.

    A source with no ACL reader returned an empty mapping, which re-sync read as
    "the source knows about none of these" and deleted the lot — quietly, on
    every refresh. It now returns None, which means "static, nothing to do".
    """
    ingestor.register(
        static_source("mail", [doc("m1", "budget", AccessControl.for_users("alice"), "mail")])
    )
    await ingestor.sync("mail")
    assert len(index) == 1

    result = await ingestor.resync_permissions("mail")

    assert result.removed == 0
    assert len(index) == 1
    assert index.search(ALICE, "budget")


async def test_a_failing_acl_read_changes_nothing(ingestor: Ingestor, index: DocumentIndex) -> None:
    """Better to serve slightly stale permissions than to delete on a blip."""

    async def explode(_ids):
        raise TimeoutError("source slow")

    ingestor.register(
        CallableSource(
            name="wiki",
            fetcher=lambda _s: _documents([doc("a", "budget", AccessControl.organisation_wide())]),
            acl_reader=explode,
        )
    )
    await ingestor.sync("wiki")

    result = await ingestor.resync_permissions("wiki")

    assert result.failed
    assert len(index) == 1


async def test_resync_of_an_empty_source_is_harmless(ingestor: Ingestor) -> None:
    ingestor.register(static_source("wiki", []))

    assert (await ingestor.resync_permissions("wiki")).ok


# -- quarantine ------------------------------------------------------------


async def test_quarantine_drops_a_sources_content(ingestor: Ingestor, index: DocumentIndex) -> None:
    """The right response to not knowing who may see a source's content."""
    ingestor.register(
        static_source(
            "wiki",
            [
                doc("a", "budget", AccessControl.organisation_wide()),
                doc("b", "plans", AccessControl.organisation_wide()),
            ],
        )
    )
    ingestor.register(
        static_source("mail", [doc("m", "note", AccessControl.for_users("alice"), "mail")])
    )
    await ingestor.sync_all()

    removed = ingestor.quarantine("wiki", reason="permission sync broken")

    assert removed == 2
    assert index.search(ALICE, "budget") == []
    assert index.search(ALICE, "note")


# -- mail as a source ------------------------------------------------------


def message(uid: str, subject: str, body: str) -> MailMessage:
    return MailMessage(
        uid=uid,
        subject=subject,
        body=body,
        from_address="cfo@corp.example",
        date=datetime(2026, 7, 27, 9, 0, tzinfo=UTC),
        external=False,
    )


async def test_a_mailbox_indexes_for_its_owner_alone(index: DocumentIndex) -> None:
    """The unambiguous case: one mailbox, exactly one reader."""
    backend = InMemoryMailBackend(
        messages=[message("1", "Q3 budget review", "Bring the headcount forecast.")]
    )
    ingestor = Ingestor(index)
    ingestor.register(build_mail_ingestion(backend, owner_id="alice"))

    await ingestor.sync("mail")

    assert index.search(ALICE, "headcount")
    assert index.search(BOB, "headcount") == []


async def test_a_mailbox_acl_names_the_person_not_a_role(index: DocumentIndex) -> None:
    """A role-based ACL would widen the moment someone joins that role."""
    backend = InMemoryMailBackend(messages=[message("1", "Private", "text")])
    ingestor = Ingestor(index)
    ingestor.register(build_mail_ingestion(backend, owner_id="alice"))
    await ingestor.sync("mail")

    acl = index.acl_of("mail:alice:1")

    assert acl.users == frozenset({"alice"})
    assert acl.groups == frozenset()


async def test_two_mailboxes_do_not_bleed(index: DocumentIndex) -> None:
    ingestor = Ingestor(index)
    ingestor.register(
        build_mail_ingestion(
            InMemoryMailBackend(messages=[message("1", "Alice budget", "alice only")]),
            owner_id="alice",
            name="mail:alice",
        )
    )
    ingestor.register(
        build_mail_ingestion(
            InMemoryMailBackend(messages=[message("1", "Bob budget", "bob only")]),
            owner_id="bob",
            name="mail:bob",
        )
    )

    await ingestor.sync_all()

    assert "alice only" in index.search(ALICE, "budget")[0].document.body
    assert "bob only" in index.search(BOB, "budget")[0].document.body
    assert len(index.search(ALICE, "budget")) == 1


async def test_a_mail_outage_reports_rather_than_empties(index: DocumentIndex) -> None:
    backend = InMemoryMailBackend(fail_with="IMAP unreachable")
    ingestor = Ingestor(index)
    ingestor.register(build_mail_ingestion(backend, owner_id="alice"))

    result = await ingestor.sync("mail")

    assert result.failed
    assert "unreachable" in result.error


async def test_resyncing_a_mailbox_keeps_it(index: DocumentIndex) -> None:
    """The static-permission path, through the real mail source."""
    backend = InMemoryMailBackend(messages=[message("1", "Budget", "text")])
    ingestor = Ingestor(index)
    ingestor.register(build_mail_ingestion(backend, owner_id="alice"))
    await ingestor.sync("mail")

    await ingestor.resync_permissions("mail")

    assert len(index) == 1


# -- bookkeeping -----------------------------------------------------------


async def test_last_sync_is_recorded(ingestor: Ingestor) -> None:
    ingestor.register(static_source("wiki", []))
    assert ingestor.last_sync("wiki") is None

    await ingestor.sync("wiki")

    assert ingestor.last_sync("wiki") is not None


async def test_a_failed_sync_does_not_advance_the_watermark(ingestor: Ingestor) -> None:
    """Otherwise the next incremental run skips what was never fetched."""

    async def explode(_since):
        raise ConnectionError("down")

    ingestor.register(CallableSource(name="wiki", fetcher=explode))

    await ingestor.sync("wiki")

    assert ingestor.last_sync("wiki") is None


async def _documents(items: list[Document]) -> list[Document]:
    return items


async def _acls(mapping: dict[str, AccessControl]) -> dict[str, AccessControl]:
    return mapping
