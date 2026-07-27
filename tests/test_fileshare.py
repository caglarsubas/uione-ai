"""File share tests, against real files with real permission bits.

Mail proved the mechanism on an unambiguous access model — one mailbox, one
reader. This is the first source where permissions are genuinely contested, and
where a naive mirror publishes things.

Nothing here is faked: files are written to a temporary directory and chmod'd,
so the derivation is tested against the operating system's actual answer rather
than against a fixture written to match my assumptions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from uione.connectors.files import (
    IdentityMap,
    build_file_ingestion,
    derive_acl,
    escapes_root,
    scan,
)
from uione.knowledge import AccessControl, DocumentIndex, Ingestor, Visibility
from uione.mcphub import Principal

UID = os.getuid()
GID = os.getgid()

OWNER = Principal(user_id="owner", roles=frozenset({"employee"}))
TEAMMATE = Principal(user_id="teammate", roles=frozenset({"shared-team", "employee"}))
OUTSIDER = Principal(user_id="outsider", roles=frozenset({"employee"}))

IDENTITIES = IdentityMap(users={UID: "owner"}, groups={GID: "shared-team"})


@pytest.fixture
def share(tmp_path: Path) -> Path:
    """A share root with realistic permissions.

    pytest's tmp_path is 0700, which the directory-chain rule correctly narrows
    to the owner — so every "world readable" assertion would fail against a
    perfectly correct implementation. Real share mounts are 0755. This caught an
    unrealistic test setup rather than a bug, which is the chain check earning
    its place.
    """
    root = tmp_path / "share"
    root.mkdir()
    root.chmod(0o755)
    return root


def write(root: Path, relative: str, text: str, mode: int = 0o644) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


# -- deriving permissions from the mode ------------------------------------


def test_a_world_readable_file_is_organisation_wide(share: Path) -> None:
    path = write(share, "notice.md", "office closed Friday", mode=0o644)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.visibility is Visibility.ORGANISATION
    assert acl.permits(OUTSIDER)


def test_a_group_readable_file_reaches_the_group_only(share: Path) -> None:
    path = write(share, "team-plan.md", "the team plan", mode=0o640)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.groups == frozenset({"shared-team"})
    assert acl.permits(TEAMMATE)
    assert not acl.permits(OUTSIDER)


def test_an_owner_only_file_reaches_the_owner_only(share: Path) -> None:
    path = write(share, "private.md", "personal notes", mode=0o600)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.users == frozenset({"owner"})
    assert acl.permits(OWNER)
    assert not acl.permits(TEAMMATE)


def test_an_unreadable_file_reaches_nobody(share: Path) -> None:
    path = write(share, "locked.md", "text", mode=0o000)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.empty


# -- the directory chain, which naive mirroring gets wrong -----------------


def test_a_private_directory_revokes_a_world_readable_file(share: Path) -> None:
    """The mistake that publishes things.

    Mode 0644 says "everyone". Inside a 0700 directory nobody but the owner can
    reach it, and mirroring the file's own bits alone would hand it to the
    organisation.
    """
    path = write(share, "private-dir/notes.md", "confidential", mode=0o644)
    (share / "private-dir").chmod(0o700)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.visibility is not Visibility.ORGANISATION
    assert not acl.permits(OUTSIDER)


def test_a_group_directory_narrows_a_world_readable_file(share: Path) -> None:
    path = write(share, "team-dir/notes.md", "team text", mode=0o644)
    (share / "team-dir").chmod(0o750)

    acl = derive_acl(path, share, IDENTITIES)

    assert not acl.permits(OUTSIDER)
    assert acl.permits(TEAMMATE)


def test_a_readable_chain_preserves_organisation_access(share: Path) -> None:
    path = write(share, "open/deeper/notes.md", "public text", mode=0o644)
    (share / "open").chmod(0o755)
    (share / "open" / "deeper").chmod(0o755)

    assert derive_acl(path, share, IDENTITIES).permits(OUTSIDER)


def test_a_directory_missing_its_execute_bit_blocks_traversal(share: Path) -> None:
    """Read without execute lists a directory but does not let you enter it."""
    path = write(share, "noexec/notes.md", "text", mode=0o644)
    (share / "noexec").chmod(0o644)

    assert not derive_acl(path, share, IDENTITIES).permits(OUTSIDER)


# -- unmapped identities ---------------------------------------------------


def test_an_unmapped_owner_yields_no_grant(share: Path) -> None:
    """A numeric id we cannot name is an id we cannot reason about."""
    path = write(share, "private.md", "text", mode=0o600)

    acl = derive_acl(path, share, IdentityMap())

    assert acl.empty


def test_an_unmapped_group_yields_no_grant(share: Path) -> None:
    path = write(share, "team.md", "text", mode=0o040)

    acl = derive_acl(path, share, IdentityMap(users={UID: "owner"}))

    assert acl.empty


def test_group_membership_is_not_our_decision(share: Path) -> None:
    """We name the group; the directory decides who is in it."""
    path = write(share, "team.md", "text", mode=0o640)

    acl = derive_acl(path, share, IDENTITIES)

    assert acl.groups == frozenset({"shared-team"})
    assert acl.users == frozenset() or "owner" in acl.users


# -- symlink escape --------------------------------------------------------


def test_a_symlink_out_of_the_share_is_detected(tmp_path: Path) -> None:
    """It would otherwise be indexed under the share's permissions."""
    outside = tmp_path.parent / "outside-secrets.md"
    outside.write_text("credentials")
    share = tmp_path / "share"
    share.mkdir()
    link = share / "innocuous.md"
    link.symlink_to(outside)

    assert escapes_root(link, share)


def test_a_symlink_within_the_share_is_allowed(tmp_path: Path) -> None:
    write(tmp_path, "real.md", "text")
    link = tmp_path / "alias.md"
    link.symlink_to(tmp_path / "real.md")

    assert not escapes_root(link, tmp_path)


def test_scanning_skips_escaping_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escaped-secret.md"
    outside.write_text("credentials for production")
    share = tmp_path / "share"
    share.mkdir()
    write(share, "ok.md", "ordinary text", mode=0o644)
    (share / "sneaky.md").symlink_to(outside)

    documents, counters = scan(share, IDENTITIES)

    assert [d.title for d in documents] == ["ok.md"]
    assert counters["skipped_escape"] == 1


# -- what gets indexed at all ----------------------------------------------


def test_binary_files_are_skipped(share: Path) -> None:
    write(share, "notes.md", "text", mode=0o644)
    binary = share / "image.md"
    binary.write_bytes(b"\x89PNG\x00\x00 renamed to look like markdown")
    binary.chmod(0o644)

    documents, counters = scan(share, IDENTITIES)

    assert [d.title for d in documents] == ["notes.md"]
    assert counters["skipped_binary"] == 1


def test_unknown_extensions_are_skipped(share: Path) -> None:
    write(share, "archive.zip", "not really a zip", mode=0o644)

    documents, counters = scan(share, IDENTITIES)

    assert documents == []
    assert counters["skipped_binary"] == 1


def test_oversized_files_are_skipped(share: Path) -> None:
    """A 200 MB log indexed whole evicts everything useful from any context."""
    from uione.connectors.files import MAX_BYTES

    write(share, "huge.log", "x" * (MAX_BYTES + 10), mode=0o644)

    documents, counters = scan(share, IDENTITIES)

    assert documents == []
    assert counters["skipped_large"] == 1


def test_files_without_a_derivable_acl_are_skipped_and_counted(share: Path) -> None:
    write(share, "ok.md", "text", mode=0o644)
    write(share, "orphan.md", "text", mode=0o600)

    documents, counters = scan(share, IdentityMap())

    assert [d.title for d in documents] == ["ok.md"]
    assert counters["skipped_acl"] == 1


def test_version_control_directories_are_ignored(share: Path) -> None:
    write(share, ".git/config.ini", "[core]", mode=0o644)
    write(share, "readme.md", "text", mode=0o644)

    documents, _ = scan(share, IDENTITIES)

    assert [d.title for d in documents] == ["readme.md"]


def test_incremental_scan_skips_unchanged_files(share: Path) -> None:
    from datetime import UTC, datetime, timedelta

    write(share, "old.md", "text", mode=0o644)

    documents, _ = scan(share, IDENTITIES, since=datetime.now(UTC) + timedelta(seconds=1))

    assert documents == []


# -- end to end through the index ------------------------------------------


async def test_the_share_reaches_the_index_with_its_permissions(share: Path) -> None:
    write(share, "public.md", "the annual leave policy changes", mode=0o644)
    write(share, "team.md", "the team roadmap for payments", mode=0o640)
    write(share, "private.md", "salary banding for the payments team", mode=0o600)

    index = DocumentIndex()
    ingestor = Ingestor(index)
    ingestor.register(build_file_ingestion(share, identities=IDENTITIES))
    await ingestor.sync("files")

    assert index.search(OUTSIDER, "leave policy")
    assert index.search(OUTSIDER, "roadmap") == []
    assert index.search(TEAMMATE, "roadmap")
    assert index.search(TEAMMATE, "salary banding") == []
    assert index.search(OWNER, "salary banding")


async def test_a_chmod_is_picked_up_by_permission_resync(share: Path) -> None:
    """A chmod changes who may read a file without changing a byte of it."""
    path = write(share, "plan.md", "the restructuring plan", mode=0o644)
    index = DocumentIndex()
    ingestor = Ingestor(index)
    ingestor.register(build_file_ingestion(share, identities=IDENTITIES))
    await ingestor.sync("files")
    assert index.search(OUTSIDER, "restructuring")

    path.chmod(0o600)
    result = await ingestor.resync_permissions("files")

    assert result.revoked == 1
    assert index.search(OUTSIDER, "restructuring") == []
    assert index.search(OWNER, "restructuring")


async def test_a_deleted_file_is_removed_on_resync(share: Path) -> None:
    path = write(share, "temp.md", "temporary notes", mode=0o644)
    index = DocumentIndex()
    ingestor = Ingestor(index)
    ingestor.register(build_file_ingestion(share, identities=IDENTITIES))
    await ingestor.sync("files")

    path.unlink()
    result = await ingestor.resync_permissions("files")

    assert result.removed == 1
    assert len(index) == 0


async def test_a_missing_share_root_fails_loudly(tmp_path: Path) -> None:
    index = DocumentIndex()
    ingestor = Ingestor(index)
    ingestor.register(build_file_ingestion(tmp_path / "absent", identities=IDENTITIES))

    result = await ingestor.sync("files")

    assert result.failed
    assert "FileNotFoundError" in result.error


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o600])
async def test_every_mode_produces_a_consistent_fingerprint(share: Path, mode: int) -> None:
    """Re-sync compares fingerprints, so identical permissions must match."""
    path = write(share, "f.md", "text", mode=mode)

    first = derive_acl(path, share, IDENTITIES)
    second = derive_acl(path, share, IDENTITIES)

    assert first.fingerprint() == second.fingerprint()


def test_the_development_identity_map_is_not_a_production_default() -> None:
    """It maps the service account, which is not an employee."""
    from uione.connectors.files import current_identity_map

    mapping = current_identity_map()

    assert set(mapping.users.values()) == {"local-user"}
    assert AccessControl.for_users("local-user").users == frozenset({"local-user"})
