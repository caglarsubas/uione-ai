"""Writing documents to the file share.

Real files, real `chmod`, real symlinks — the same discipline as the read side,
because the interesting failures here are all filesystem failures.

The assertion this module exists for is the last one in the file: a document can
be written successfully and be readable by nobody, and the only way anyone finds
out is if the tool says so.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from uione.connectors.files import (
    MAX_DOCUMENT_BYTES,
    DocumentWriter,
    IdentityMap,
    build_document_source,
    safe_filename,
)
from uione.mcphub import Principal, RiskClass

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))


@pytest.fixture
def share(tmp_path: Path) -> Path:
    """A share the world can traverse.

    pytest's `tmp_path` is 0700, and the directory-chain rule correctly narrows
    everything beneath it to its owner — which made an earlier suite's every
    "readable" assertion fail against entirely correct code.
    """
    root = tmp_path / "share"
    root.mkdir()
    root.chmod(0o755)
    return root


@pytest.fixture
def identities() -> IdentityMap:
    return IdentityMap(users={os.getuid(): "alice"}, groups={os.getgid(): "payments-team"})


@pytest.fixture
def writer(share: Path, identities: IdentityMap) -> DocumentWriter:
    return DocumentWriter(share, identities=identities)


# -- filenames -------------------------------------------------------------


def test_a_title_becomes_a_readable_filename() -> None:
    assert safe_filename("Settlement incident review") == "Settlement-incident-review.md"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/cron.d/x",
        "../../secrets",
        "/etc/passwd",
        "..\\..\\windows",
        "a/b/c",
    ],
)
def test_a_traversing_title_cannot_produce_a_traversing_filename(hostile: str) -> None:
    """A title is attacker-influenced text — it can arrive from an email the
    model was summarising."""
    name = safe_filename(hostile)

    assert "/" not in name
    assert "\\" not in name
    assert not name.startswith(".")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Ödeme mutabakatı", "Odeme-mutabakati.md"),
        ("Çağlar toplantı", "Caglar-toplanti.md"),
        ("Straße größe", "Strasse-grosse.md"),
        ("Łódź raport", "Lodz-raport.md"),
    ],
)
def test_letters_unicode_cannot_decompose_are_transliterated(title: str, expected: str) -> None:
    """NFKD splits accented letters into a base plus a mark, so é and ö survive.
    It does nothing for letters that are not accented forms of anything —
    Turkish dotless ı, German ß, Polish ł — which then vanish at the ASCII step.

    Without the transliteration table these produced "Odeme-mutabakat",
    "Strae" and "odz". A filename that silently drops letters from the author's
    own language is a document they cannot find again.
    """
    assert safe_filename(title) == expected


def test_a_title_of_pure_punctuation_still_produces_a_filename() -> None:
    assert safe_filename("!!!***") == "document.md"


def test_a_very_long_title_is_truncated() -> None:
    name = safe_filename("x" * 400)

    assert len(name) <= 130


# -- writing ---------------------------------------------------------------


def test_a_document_lands_in_the_share(writer: DocumentWriter) -> None:
    written = writer.write("Runbook", "Restart the settlement worker.", author="alice")

    assert written.path.exists()
    assert "Restart the settlement worker." in written.path.read_text()
    assert written.relative == "documents/Runbook.md"


def test_the_document_says_where_it_came_from(writer: DocumentWriter) -> None:
    """A document that looks hand-written is one nobody can audit later."""
    written = writer.write("Runbook", "Body.", author="alice")

    text = written.path.read_text()
    assert "generated_by: UiOne assistant" in text
    assert "requested_by: alice" in text


def test_an_existing_document_is_never_overwritten(writer: DocumentWriter) -> None:
    """Overwriting destroys work with no undo, and silently writing `report-2.md`
    is the kind of helpfulness that loses a document."""
    writer.write("Runbook", "First version.")

    with pytest.raises(FileExistsError, match="already exists"):
        writer.write("Runbook", "Second version.")

    assert "First version." in (writer.directory / "Runbook.md").read_text()


def test_an_empty_document_is_refused(writer: DocumentWriter) -> None:
    """A zero-byte file with a confident filename looks like success."""
    with pytest.raises(ValueError, match="needs a body"):
        writer.write("Empty", "   \n  ")


def test_an_enormous_document_is_refused(writer: DocumentWriter) -> None:
    """A model in a loop writing a gigabyte into a share is a plausible Tuesday."""
    with pytest.raises(ValueError, match="limit"):
        writer.write("Huge", "x" * (MAX_DOCUMENT_BYTES + 1))


def test_writing_creates_the_folder_once(writer: DocumentWriter) -> None:
    writer.write("One", "a")
    writer.write("Two", "b")

    assert writer.directory.is_dir()
    assert len(list(writer.directory.iterdir())) == 2


# -- staying inside the share ----------------------------------------------


def test_a_traversing_title_writes_inside_the_share(writer: DocumentWriter, share: Path) -> None:
    written = writer.write("../../../etc/cron.d/payload", "malicious")

    assert written.path.resolve().is_relative_to(share.resolve())


def test_a_symlinked_documents_folder_is_refused(
    share: Path, tmp_path: Path, identities: IdentityMap
) -> None:
    """The other half of the traversal attack: the filename is clean but the
    directory it lands in points somewhere else."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (share / "documents").symlink_to(outside)

    writer = DocumentWriter(share, identities=identities)

    with pytest.raises(ValueError, match="outside the share"):
        writer.write("Escape", "content")

    assert not list(outside.iterdir())


# -- the permissions question ----------------------------------------------


def test_the_written_document_reports_who_can_read_it(writer: DocumentWriter) -> None:
    written = writer.write("Runbook", "Body.")

    # World-readable collapses to ORGANISATION rather than enumerating every
    # user, which is the ACL model's answer and not a gap in it.
    assert not written.readable_by_nobody
    assert written.acl.visibility.value == "organisation" or "alice" in written.acl.users


def test_a_document_in_an_unreachable_folder_is_reported_as_unreadable(
    share: Path, identities: IdentityMap
) -> None:
    """The failure this whole module exists to make visible.

    The write succeeds. The file exists. Its own mode is fine. And nobody but
    the service account can reach it, because a directory above it is not
    traversable — which is exactly the situation an assistant running as a
    service account creates by default, and exactly what the author will not
    discover until somebody needs the document.
    """
    private = share / "documents"
    private.mkdir()
    private.chmod(0o700)

    # An identity map that knows the group but *not* the owning user, standing
    # in for the ordinary case: the file is owned by the service account, and
    # the people who need it are elsewhere.
    strangers = IdentityMap(users={}, groups={os.getgid(): "payments-team"})
    written = DocumentWriter(share, identities=strangers).write("Secret", "Body.")

    assert written.path.exists()
    assert written.readable_by_nobody


def test_a_group_readable_document_names_the_group(share: Path, identities: IdentityMap) -> None:
    """0640 is the interesting mode: the group can read it and the world cannot,
    so the ACL must name the group rather than collapsing to organisation-wide.
    """
    from uione.connectors.files.acl import derive_acl

    documents = share / "documents"
    documents.mkdir()
    documents.chmod(0o750)

    written = DocumentWriter(share, identities=identities).write("Shared", "Body.")
    written.path.chmod(0o640)

    acl = derive_acl(written.path, share, identities)

    assert "payments-team" in acl.groups
    assert acl.visibility.value != "organisation"


# -- through the tool ------------------------------------------------------


async def test_the_tool_writes_and_says_who_can_read(writer: DocumentWriter) -> None:
    source = build_document_source(writer)
    source.bind_principal("principal", ALICE)

    result = await source.call(
        "write_document", {"title": "Postmortem PAY-1182", "body": "What happened."}
    )

    assert result.ok
    assert "documents/Postmortem-PAY-1182.md" in result.content
    assert "Readable by" in result.content
    assert result.structured["readable_by_nobody"] is False


async def test_unreadability_is_a_field_not_only_prose(
    share: Path, identities: IdentityMap
) -> None:
    """Models omit caveats they were told to include — the recurring finding in
    docs/EVALS.md. The UI renders this field rather than trusting the sentence.
    """
    private = share / "documents"
    private.mkdir()
    private.chmod(0o700)
    writer = DocumentWriter(share, identities=IdentityMap(users={}, groups={}))
    source = build_document_source(writer)

    result = await source.call("write_document", {"title": "Orphan", "body": "Body."})

    assert result.ok
    assert result.structured["readable_by_nobody"] is True
    assert "unreachable" in result.content


async def test_a_missing_title_is_refused(writer: DocumentWriter) -> None:
    result = await build_document_source(writer).call("write_document", {"body": "Body."})

    assert not result.ok
    assert "title" in (result.error or "")


async def test_a_collision_is_reported_rather_than_silently_renamed(
    writer: DocumentWriter,
) -> None:
    source = build_document_source(writer)
    await source.call("write_document", {"title": "Notes", "body": "First."})

    result = await source.call("write_document", {"title": "Notes", "body": "Second."})

    assert not result.ok
    assert "already exists" in (result.error or "")


async def test_a_read_only_share_fails_as_a_result_not_a_traceback(
    tmp_path: Path, identities: IdentityMap
) -> None:
    """A full disk, a read-only mount, a permission the service account lacks."""
    root = tmp_path / "readonly"
    root.mkdir()
    root.chmod(0o555)
    source = build_document_source(DocumentWriter(root, identities=identities))

    try:
        result = await source.call("write_document", {"title": "Nope", "body": "Body."})
        assert not result.ok
        assert "could not write" in (result.error or "")
    finally:
        # Restored so pytest can remove it — it cannot delete what it cannot
        # enter, and the teardown failure would be blamed on the next test.
        root.chmod(0o755)


async def test_writing_is_reversible_because_it_never_overwrites(
    writer: DocumentWriter,
) -> None:
    """Deleting the new file restores the world exactly. That is what makes
    this REVERSIBLE_WRITE rather than something that can never run unattended."""
    specs = {s.tool: s for s in await build_document_source(writer).list_tools()}

    assert specs["write_document"].risk is RiskClass.REVERSIBLE_WRITE


async def test_a_written_document_can_be_found_by_search(
    share: Path, identities: IdentityMap
) -> None:
    """The point of writing into the share rather than a private store: the
    document is indexed by the same pipeline, under permissions derived from
    where it actually landed."""
    from uione.connectors.files import build_file_ingestion
    from uione.knowledge import DocumentIndex, Ingestor

    (share / "documents").mkdir()
    (share / "documents").chmod(0o755)
    written = DocumentWriter(share, identities=identities).write(
        "Settlement postmortem", "The acquirer changed a timeout."
    )
    written.path.chmod(0o644)

    index = DocumentIndex()
    ingestor = Ingestor(index)
    ingestor.register(build_file_ingestion(share, identities=identities))
    await ingestor.sync("files")

    hits = index.search(Principal(user_id="alice", roles=frozenset()), "acquirer timeout")

    assert hits, "a document written by the assistant must be findable afterwards"


def test_the_write_and_read_size_limits_are_named_separately() -> None:
    """Writing and reading have no reason to share a limit, and one name for
    both would make changing either surprising."""
    from uione.connectors.files import source

    assert hasattr(source, "MAX_BYTES")
    assert MAX_DOCUMENT_BYTES > 0


@pytest.fixture(autouse=True)
def _restore_modes(tmp_path: Path):
    """Leave nothing pytest cannot delete.

    Walk top-down: a bottom-up pass never reaches files inside a directory that
    is missing its execute bit, which is how an earlier suite left 25 undeletable
    temporary directories behind.
    """
    yield
    for current, directories, files in os.walk(tmp_path):
        for name in (*directories, *files):
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IRWXU)
            except OSError:
                pass
