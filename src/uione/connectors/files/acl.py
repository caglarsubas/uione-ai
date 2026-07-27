"""Deriving access control from POSIX file permissions.

The first source whose permissions are not trivially "one person". A file share
has owners, groups, and — the part naive mirroring gets wrong — a *containing
directory chain* that can make a world-readable file unreachable.

The mapping itself is small. What matters is the three ways it is easy to get
wrong, each of which widens access:

**Directory traversal.** A file with mode 0644 looks readable by everyone. If its
parent directory is 0700, nobody but the owner can reach it. Mirroring the file's
own bits alone publishes it. So the effective ACL is the *intersection* down the
chain, not the file's own mode.

**Group membership is the identity system's answer, not ours.** We map a gid to a
group name and stop. Deciding who is in that group is the directory's job, and an
index that keeps its own opinion of membership will drift.

**Unresolvable means excluded.** An unmapped uid or gid produces no grant rather
than a permissive default. A numeric id we cannot name is an id we cannot reason
about.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from uione.knowledge.documents import AccessControl, Visibility

log = structlog.get_logger(__name__)

# POSIX read bits.
OWNER_READ = stat.S_IRUSR
GROUP_READ = stat.S_IRGRP
OTHER_READ = stat.S_IROTH

# Directories additionally need the execute bit to be traversable.
OWNER_TRAVERSE = stat.S_IRUSR | stat.S_IXUSR
GROUP_TRAVERSE = stat.S_IRGRP | stat.S_IXGRP
OTHER_TRAVERSE = stat.S_IROTH | stat.S_IXOTH


@dataclass
class IdentityMap:
    """Translates numeric ids into the names the rest of the system uses.

    Explicit rather than read from ``/etc/passwd``: the account running the
    connector is rarely the account model the *application* authenticates
    against, and silently equating them is how a local service account acquires
    an employee's permissions.
    """

    users: dict[int, str] = field(default_factory=dict)
    groups: dict[int, str] = field(default_factory=dict)

    def user(self, uid: int) -> str | None:
        return self.users.get(uid)

    def group(self, gid: int) -> str | None:
        return self.groups.get(gid)


@dataclass(frozen=True)
class PathPermissions:
    """The read grants a single path carries, before the chain is considered."""

    owner_uid: int
    group_gid: int
    owner_can_read: bool
    group_can_read: bool
    others_can_read: bool

    @classmethod
    def of(cls, path: Path, *, directory: bool) -> PathPermissions:
        info = path.stat()
        mode = info.st_mode
        # A directory must be executable as well as readable to be entered.
        owner_mask = OWNER_TRAVERSE if directory else OWNER_READ
        group_mask = GROUP_TRAVERSE if directory else GROUP_READ
        other_mask = OTHER_TRAVERSE if directory else OTHER_READ
        return cls(
            owner_uid=info.st_uid,
            group_gid=info.st_gid,
            owner_can_read=(mode & owner_mask) == owner_mask,
            group_can_read=(mode & group_mask) == group_mask,
            others_can_read=(mode & other_mask) == other_mask,
        )


def derive_acl(path: Path, root: Path, identities: IdentityMap) -> AccessControl:
    """Effective read permissions for a file, including its directory chain.

    The intersection down the chain, not the file's own mode. A 0644 file inside
    a 0700 directory is readable by its owner alone, and mirroring only the file
    would publish it to the organisation.
    """
    try:
        file_perms = PathPermissions.of(path, directory=False)
    except OSError as exc:
        log.warning("files.stat_failed", path=str(path), error=type(exc).__name__)
        return AccessControl()

    others = file_perms.others_can_read
    group_gids: set[int] = {file_perms.group_gid} if file_perms.group_can_read else set()
    owner_uids: set[int] = {file_perms.owner_uid} if file_perms.owner_can_read else set()

    # Walk up to the root, narrowing at each step.
    for parent in _chain(path.parent, root):
        try:
            perms = PathPermissions.of(parent, directory=True)
        except OSError:
            # A directory we cannot inspect is a chain we cannot verify.
            return AccessControl()

        if not perms.others_can_read:
            others = False
        if not perms.group_can_read:
            group_gids &= {perms.group_gid} if perms.group_can_read else set()
        # The owner of a parent directory is not necessarily the owner of the
        # file; only a traversable chain preserves the file owner's access.
        if not (perms.owner_can_read or perms.group_can_read or perms.others_can_read):
            owner_uids = set()

    if others:
        # Anything world-readable through the whole chain is organisation-wide.
        # Not "public": the file share is already inside the perimeter.
        return AccessControl(visibility=Visibility.ORGANISATION)

    users = {name for uid in owner_uids if (name := identities.user(uid))}
    groups = {name for gid in group_gids if (name := identities.group(gid))}

    if not users and not groups:
        # Either nothing may read it, or the ids are unmapped. Both mean we
        # cannot say who may read it, and the answer to that is nobody.
        return AccessControl()

    return AccessControl(users=frozenset(users), groups=frozenset(groups))


def _chain(start: Path, root: Path) -> list[Path]:
    """Directories from ``start`` up to and including ``root``."""
    chain: list[Path] = []
    current = start.resolve()
    root = root.resolve()
    while True:
        chain.append(current)
        if current == root or current == current.parent:
            break
        current = current.parent
    return chain


def escapes_root(path: Path, root: Path) -> bool:
    """Whether a path resolves outside the configured root.

    Symlinks are the reason this exists. A link inside the share pointing at
    ``/etc`` or another user's home would otherwise be indexed with the share's
    permissions, publishing content whose real ACL was never consulted.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return True
    return False


def current_identity_map() -> IdentityMap:
    """The running process's own ids, for development only.

    Convenient locally and wrong in production, where the service account is not
    an employee. A deployment supplies the real mapping.
    """
    return IdentityMap(users={os.getuid(): "local-user"}, groups={os.getgid(): "local-group"})
