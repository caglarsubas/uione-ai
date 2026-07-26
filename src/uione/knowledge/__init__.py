"""Knowledge layer — the work graph, retrieval, and memory."""

from uione.knowledge.entities import EntityKind, EntityRef, GraphItem, entity, normalise_key
from uione.knowledge.extract import ExtractionRules, extract_entities, extract_message_ids
from uione.knowledge.graph import Cluster, Link, WorkGraph

__all__ = [
    "Cluster",
    "EntityKind",
    "EntityRef",
    "ExtractionRules",
    "GraphItem",
    "Link",
    "WorkGraph",
    "entity",
    "extract_entities",
    "extract_message_ids",
    "normalise_key",
]
