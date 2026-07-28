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
from uione.connectors.files.write import (
    MAX_DOCUMENT_BYTES,
    DocumentWriter,
    WrittenDocument,
    build_document_source,
    safe_filename,
)

__all__ = [
    "MAX_DOCUMENT_BYTES",
    "DocumentWriter",
    "WrittenDocument",
    "build_document_source",
    "safe_filename",
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
