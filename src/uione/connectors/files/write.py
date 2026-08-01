"""Writing a document to the file share.

The last capability in the original brief, and the one where the write itself is
the easy part. The hard part is the sentence nobody asks for and everybody
needs: **who can read this now?**

That question has a real answer here and it is frequently not the one the author
assumed. The assistant runs as a service account, so a file it creates is owned
by *that* account with *that* umask, in a directory whose group and mode it did
not choose. Alice asks for a document; the file lands owned by `uione`, group
`staff`, mode 0640, in a folder her team cannot traverse — and she is told it was
saved. She finds out it is unreadable when somebody needs it.

So this connector derives the ACL of the file it just wrote, from the filesystem,
and reports it. Not the ACL it intended — the one that exists. When those differ
the difference is the message.

**Four refusals.**

*Outside the root, never.* A title is attacker-influenced text — it can arrive
from an email the model was summarising — and `../../../etc/cron.d/x` is a
document name. Both the assembled path and its resolution are checked, because
a symlink inside the share is the other half of the same attack.

*An existing file, never.* Overwriting destroys work with no undo, and silently
writing `report-2.md` when somebody asked for `report.md` is the kind of
helpfulness that loses a document.

*Nothing enormous.* A model in a loop writing a gigabyte into a share is a
plausible Tuesday.

*No empty documents.* A zero-byte file with a confident filename is worse than
an error, because it looks like success.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from uione.connectors.files.acl import IdentityMap, derive_acl, escapes_root
from uione.knowledge.documents import AccessControl
from uione.mcphub import InMemoryToolSource, Principal, RiskClass, ToolResult

log = structlog.get_logger(__name__)

#: Largest document this will write. Distinct from the scanner's MAX_BYTES,
#: which bounds what is *read*: writing and reading have no reason to share a
#: limit, and one name for both would make changing either surprising.
MAX_DOCUMENT_BYTES = 512 * 1024

#: Characters a filename may keep. Everything else becomes a hyphen — including
#: the separators that make traversal possible in the first place.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_DASHES = re.compile(r"-{2,}")

#: Letters Unicode decomposition will not help with.
#:
#: NFKD splits an accented letter into a base plus a combining mark, so é and ö
#: survive as e and o. It does nothing for letters that are not accented forms
#: of anything — Turkish dotless ı, German ß, Polish ł, Nordic ø and æ,
#: Icelandic þ and ð — which then vanish entirely at the ASCII step.
#:
#: The result is not cosmetic. Without this table "Ödeme mutabakatı" becomes
#: "Odeme-mutabakat", "Straße" becomes "Strae", and "Łódź" becomes "odz". A
#: filename that silently drops letters from the author's own language is a
#: document they cannot find again.
_TRANSLITERATE = str.maketrans(
    {
        "ı": "i",
        "İ": "I",
        "ğ": "g",
        "Ğ": "G",
        "ş": "s",
        "Ş": "S",
        "ß": "ss",
        "ẞ": "SS",
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "þ": "th",
        "Þ": "TH",
        "ð": "d",
        "Ð": "D",
        "đ": "d",
        "Đ": "D",
    }
)


@dataclass(frozen=True)
class WrittenDocument:
    path: Path
    relative: str
    acl: AccessControl
    bytes_written: int

    @property
    def readable_by_nobody(self) -> bool:
        return self.acl.empty


def safe_filename(title: str, *, extension: str = ".md") -> str:
    """Turn a title into a filename that cannot leave its directory.

    Accents are folded rather than stripped, so a Turkish or German title stays
    recognisable instead of becoming a row of hyphens. Everything structural —
    slashes, dots in sequence, control characters — is replaced, which is what
    makes traversal impossible before any path is assembled.
    """
    # Transliterate first: NFKD cannot help with letters that are not accented
    # forms, and the ASCII step below would drop them silently.
    normalised = (
        unicodedata.normalize("NFKD", title.translate(_TRANSLITERATE))
        .encode("ascii", "ignore")
        .decode()
    )
    cleaned = _UNSAFE.sub("-", normalised).strip(" .-")
    cleaned = _DASHES.sub("-", cleaned).replace(" ", "-")

    # A title of nothing but punctuation, or of characters that all folded away.
    if not cleaned:
        cleaned = "document"

    # Leaving room for the extension and for a filesystem that stops at 255.
    return cleaned[:120] + extension


class DocumentWriter:
    """Writes documents into one directory of the share, and no other."""

    def __init__(
        self,
        root: str | Path,
        *,
        identities: IdentityMap,
        folder: str = "documents",
    ) -> None:
        self._root = Path(root)
        self._identities = identities
        self._folder = folder

    @property
    def directory(self) -> Path:
        return self._root / self._folder

    def write(self, title: str, body: str, *, author: str = "") -> WrittenDocument:
        if not body.strip():
            raise ValueError("a document needs a body")

        encoded = body.encode("utf-8")
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise ValueError(
                f"document is {len(encoded) // 1024}KB; the limit is {MAX_DOCUMENT_BYTES // 1024}KB"
            )

        target = self.directory / safe_filename(title)

        # Checked before the directory is created, so a hostile title cannot
        # cause a directory to appear somewhere it should not.
        if not _within(target, self._root):
            raise ValueError("refusing to write outside the share")

        self.directory.mkdir(parents=True, exist_ok=True)

        # Re-checked after mkdir, because the directory may itself be — or have
        # become — a symlink pointing elsewhere. The first check was on the
        # assembled path; this one is on what the filesystem resolves.
        if escapes_root(self.directory, self._root):
            raise ValueError("refusing to write outside the share")

        if target.exists():
            raise FileExistsError(
                f"{target.name} already exists; choose another title rather than overwriting it"
            )

        document = _with_front_matter(title, body, author=author)

        # "x" mode: create, and fail if it appeared between the check above and
        # now. The gap is small and somebody eventually loses a document in it.
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(document)

        written = len(document.encode("utf-8"))
        acl = derive_acl(target, self._root, self._identities)

        log.info(
            "files.document_written",
            path=str(target.relative_to(self._root)),
            bytes=written,
            readable_by=len(acl.users) + len(acl.groups),
        )
        return WrittenDocument(
            path=target,
            relative=str(target.relative_to(self._root)),
            acl=acl,
            bytes_written=written,
        )


def _within(path: Path, root: Path) -> bool:
    """Whether an *unresolved* path stays inside the root.

    Deliberately not `resolve()`: this runs before the file exists, and the
    point is to reject `../` in the assembled path itself. Symlinks are the
    other check's job.
    """
    try:
        os.path.relpath(path, root)
        return Path(os.path.normpath(path)).is_relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False


def _with_front_matter(title: str, body: str, *, author: str) -> str:
    """A small header, so the document says where it came from.

    An assistant-written document that looks hand-written is one nobody can
    audit later. The provenance line is the difference between "we wrote this"
    and "something wrote this".
    """
    lines = [
        "---",
        f"title: {title}",
        f"generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "generated_by: UiOne assistant",
    ]
    if author:
        lines.append(f"requested_by: {author}")
    lines += ["---", "", body.rstrip(), ""]
    return "\n".join(lines)


# -- the governed tool -----------------------------------------------------


def build_document_source(writer: DocumentWriter, *, name: str = "documents") -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def write_document(args: dict, principal: Principal) -> ToolResult:
        title = str(args.get("title", "")).strip()
        body = str(args.get("body", ""))

        if not title:
            return ToolResult.failure("title is required")

        # Stamped into the document's front matter, so `requested_by` names the
        # person who asked rather than whoever the gateway most recently served.
        author = principal.user_id

        try:
            written = writer.write(title, body, author=author)
        except FileExistsError as exc:
            return ToolResult.failure(str(exc))
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        except OSError as exc:
            # A full disk, a read-only mount, a permission the service account
            # does not have. Named as what it is rather than as a traceback.
            return ToolResult.failure(f"could not write the document: {exc.strerror or exc}")

        return ToolResult.success(
            f"Saved {written.relative} ({written.bytes_written} bytes).\n{_describe(written)}",
            {
                "path": written.relative,
                "bytes": written.bytes_written,
                "readable_by_users": sorted(written.acl.users),
                "readable_by_groups": sorted(written.acl.groups),
                "visibility": written.acl.visibility.value,
                # Surfaced as a field, not left to prose. The recurring finding
                # in docs/EVALS.md is that models omit caveats they were told to
                # include; the UI renders this rather than trusting the sentence.
                "readable_by_nobody": written.readable_by_nobody,
            },
        )

    source.register(
        "write_document",
        write_document,
        description=(
            "Write a document to the file share as Markdown. "
            "Reports who can read it afterwards — which is often not who you expect."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Becomes the filename."},
                "body": {"type": "string", "description": "Markdown."},
            },
            "required": ["title", "body"],
        },
        # It creates a new file and refuses to overwrite, so deleting it
        # restores the world exactly. That is what reversible means here.
        risk=RiskClass.REVERSIBLE_WRITE,
        identified=True,
    )
    return source


def _describe(written: WrittenDocument) -> str:
    """Say who can read it, in a sentence somebody would act on."""
    if written.readable_by_nobody:
        # The failure this whole module exists to make visible.
        return (
            "Nobody can currently read it: the file's permissions, or those of a "
            "directory above it, exclude everyone. It is saved but unreachable."
        )

    audiences: list[str] = []
    if written.acl.visibility.value == "organisation":
        audiences.append("everyone in the organisation")
    if users := sorted(written.acl.users):
        audiences.append("users " + ", ".join(users))
    if groups := sorted(written.acl.groups):
        audiences.append("groups " + ", ".join(groups))

    return "Readable by " + "; ".join(audiences) + "."
