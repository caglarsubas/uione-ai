"""Knowledge layer — the work graph, permission-aware retrieval, and memory."""

from uione.knowledge.documents import AccessControl, Document, Visibility
from uione.knowledge.entities import EntityKind, EntityRef, GraphItem, entity, normalise_key
from uione.knowledge.extract import ExtractionRules, extract_entities, extract_message_ids
from uione.knowledge.graph import Cluster, Link, WorkGraph
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
from uione.knowledge.source import build_knowledge_source

__all__ = [
    "STOPWORDS",
    "AccessControl",
    "Cluster",
    "Document",
    "DocumentIndex",
    "EntityKind",
    "EntityRef",
    "ExtractionRules",
    "GraphItem",
    "IndexStats",
    "Link",
    "SearchHit",
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
    "tokenize",
]
