"""File share connector — documents with real group permissions."""

from uione.connectors.files.acl import (
    IdentityMap,
    PathPermissions,
    current_identity_map,
    derive_acl,
    escapes_root,
)
from uione.connectors.files.source import (
    MAX_BYTES,
    TEXT_SUFFIXES,
    build_file_ingestion,
    scan,
)

__all__ = [
    "MAX_BYTES",
    "TEXT_SUFFIXES",
    "IdentityMap",
    "PathPermissions",
    "build_file_ingestion",
    "current_identity_map",
    "derive_acl",
    "escapes_root",
    "scan",
]
