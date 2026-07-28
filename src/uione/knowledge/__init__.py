"""Knowledge layer — the work graph, permission-aware retrieval, and memory."""

from uione.knowledge.documents import AccessControl, Document, Visibility
from uione.knowledge.entities import EntityKind, EntityRef, GraphItem, entity, normalise_key
from uione.knowledge.extract import ExtractionRules, extract_entities, extract_message_ids
from uione.knowledge.graph import Cluster, Link, WorkGraph
from uione.knowledge.groups import FLAT, MAX_DEPTH, GroupGraph
from uione.knowledge.hierarchy import MAX_ANCESTRY, AclNode, Hierarchy, principal_groups
from uione.knowledge.index import (
    STOPWORDS,
    DocumentIndex,
    IndexStats,
    SearchHit,
    tokenize,
)
from uione.knowledge.ingest import (
    CallableSource,
    IngestionSource,
    Ingestor,
    SyncResult,
)
from uione.knowledge.mail_source import build_mail_ingestion
from uione.knowledge.refresh import (
    IngestionRefresher,
    RefreshStats,
    SourceHealth,
)
from uione.knowledge.semantic import (
    BATCH_SIZE,
    MIN_SIMILARITY,
    RRF_K,
    SEMANTIC_WEIGHT,
    Embedder,
    HybridResult,
    HybridSearch,
    SemanticHit,
    VectorIndex,
    content_hash,
    cosine,
    fuse,
)
from uione.knowledge.source import build_knowledge_source

__all__ = [
    "BATCH_SIZE",
    "MIN_SIMILARITY",
    "SEMANTIC_WEIGHT",
    "RRF_K",
    "Embedder",
    "HybridResult",
    "HybridSearch",
    "SemanticHit",
    "VectorIndex",
    "content_hash",
    "cosine",
    "fuse",
    "STOPWORDS",
    "FLAT",
    "MAX_ANCESTRY",
    "MAX_DEPTH",
    "AccessControl",
    "AclNode",
    "Cluster",
    "Document",
    "DocumentIndex",
    "EntityKind",
    "EntityRef",
    "ExtractionRules",
    "GraphItem",
    "GroupGraph",
    "Hierarchy",
    "IndexStats",
    "IngestionRefresher",
    "Link",
    "RefreshStats",
    "SearchHit",
    "SourceHealth",
    "Visibility",
    "WorkGraph",
    "CallableSource",
    "IngestionSource",
    "Ingestor",
    "SyncResult",
    "build_knowledge_source",
    "build_mail_ingestion",
    "entity",
    "extract_entities",
    "extract_message_ids",
    "normalise_key",
    "principal_groups",
    "tokenize",
]
