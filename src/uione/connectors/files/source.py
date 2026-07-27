"""A file share as an ingestion source.

Walks a directory tree, indexes readable text, and derives each document's ACL
from the filesystem. Unlike mail — one mailbox, one reader — this is the first
source where permissions are genuinely contested: owners, groups, and a directory
chain that can revoke what a file's own mode appears to grant.

What it deliberately does not do: guess. A file whose permissions cannot be
resolved is skipped, a symlink leaving the share is skipped, and a file that is
not plainly text is skipped. Each of those is a case where indexing the thing
anyway would be a permissions decision made by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import structlog

from uione.connectors.files.acl import IdentityMap, derive_acl, escapes_root
from uione.knowledge.documents import AccessControl, Document
from uione.knowledge.ingest import CallableSource

log = structlog.get_logger(__name__)

#: Extensions indexed as text. An allowlist, not a denylist: a new binary format
#: appearing in the share should be ignored by default rather than parsed as
#: mojibake and indexed.
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json", ".yaml", ".yml", ".ini", ".cfg"}
)

#: Files above this are skipped. A 200 MB log indexed whole would evict
#: everything useful from any context it reaches.
MAX_BYTES = 1_000_000

SKIP_DIRECTORIES = frozenset({".git", ".svn", "node_modules", "__pycache__", ".venv"})


def _looks_like_text(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        head = path.open("rb").read(1024)
    except OSError:
        return False
    # A NUL byte in the first kilobyte means it is not the text its extension
    # claims — a renamed archive, or a file being written.
    return b"\x00" not in head


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("files.read_failed", path=str(path), error=type(exc).__name__)
        return None


def scan(
    root: Path, identities: IdentityMap, *, since: datetime | None = None
) -> tuple[list[Document], dict[str, int]]:
    """Walk the share and build documents with their derived permissions."""
    root = root.resolve()
    documents: list[Document] = []
    counters = {"skipped_binary": 0, "skipped_large": 0, "skipped_escape": 0, "skipped_acl": 0}

    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.is_dir():
            continue

        if escapes_root(path, root):
            # A symlink out of the share would be indexed under the share's
            # permissions, publishing content whose real ACL was never consulted.
            counters["skipped_escape"] += 1
            log.warning("files.escapes_root", path=str(path))
            continue

        if not _looks_like_text(path):
            counters["skipped_binary"] += 1
            continue

        try:
            info = path.stat()
        except OSError:
            continue

        if info.st_size > MAX_BYTES:
            counters["skipped_large"] += 1
            continue

        modified = datetime.fromtimestamp(info.st_mtime, tz=UTC)
        if since is not None and modified <= since:
            continue

        acl = derive_acl(path, root, identities)
        if acl.empty:
            # Skipped rather than indexed-and-hidden: an operator seeing a large
            # count here has a permission-mapping problem to fix, not a quiet
            # corpus of unreachable files.
            counters["skipped_acl"] += 1
            log.info("files.no_derivable_acl", path=str(path.relative_to(root)))
            continue

        body = _read_text(path)
        if body is None:
            continue

        relative = path.relative_to(root)
        documents.append(
            Document(
                id=f"file:{relative}",
                title=str(relative),
                body=body,
                source="files",
                acl=acl,
                url=str(path),
                updated_at=modified,
                metadata={"bytes": info.st_size, "acl": acl.fingerprint()},
            )
        )

    return documents, counters


def build_file_ingestion(
    root: str | Path, *, identities: IdentityMap, name: str = "files"
) -> CallableSource:
    """A file share, with permissions read from the filesystem."""
    root_path = Path(root)

    async def fetch(since: datetime | None) -> list[Document]:
        if not root_path.exists():
            raise FileNotFoundError(f"file share root not found: {root_path}")
        documents, counters = scan(root_path, identities, since=since)
        skipped = {k: v for k, v in counters.items() if v}
        if skipped:
            log.info("files.scan_complete", indexed=len(documents), **skipped)
        return documents

    async def acls(document_ids: list[str]) -> dict[str, AccessControl]:
        """Re-read permissions from disk without re-reading content.

        The cheap half of a re-sync: a chmod is invisible to a content hash but
        changes who may read the file, and that change has a deadline.
        """
        current: dict[str, AccessControl] = {}
        for document_id in document_ids:
            relative = document_id.removeprefix("file:")
            path = root_path / relative
            if not path.exists() or escapes_root(path, root_path):
                # Absent from the mapping means "gone", which removes it.
                continue
            acl = derive_acl(path, root_path, identities)
            if not acl.empty:
                current[document_id] = acl
        return current

    return CallableSource(name=name, fetcher=fetch, acl_reader=acls)
