"""Retrieval exposed as a governed MCP tool.

The gateway passes the calling principal into the handler, so the tool receives
the identity it must filter by rather than inferring one. That is the whole
integration: there is no path from a tool call to the index that does not carry
a principal, and no way to reach the index with somebody else's.
"""

from __future__ import annotations

import structlog

from uione.knowledge.index import DocumentIndex
from uione.mcphub import InMemoryToolSource, Principal, RiskClass, ToolResult

log = structlog.get_logger(__name__)

MAX_RESULTS = 10


def build_knowledge_source(
    index: DocumentIndex, *, name: str = "knowledge", hybrid=None
) -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def search(args: dict, principal: Principal) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.failure("query is required")

        try:
            limit = max(1, min(int(args.get("limit", 5)), MAX_RESULTS))
        except (TypeError, ValueError):
            limit = 5

        # Hybrid when this deployment has embeddings, lexical otherwise. The
        # fallback is not an error path: a deployment with no embedding model
        # still gets working search.
        note = ""
        if hybrid is not None:
            result = await hybrid.search(principal, query, limit=limit)
            hits, note = result.hits, result.note
        else:
            hits = index.search(principal, query, limit=limit)

        if not hits:
            # Identical wording whether nothing matched or nothing was permitted:
            # the difference is exactly what must not be observable.
            return ToolResult.success(
                f"No documents matching {query!r}." + (f" ({note})" if note else ""),
                {"count": 0, "semantic": not note},
            )

        return ToolResult.success(
            "\n".join(h.render() for h in hits) + (f"\n({note})" if note else ""),
            # `semantic` is a field rather than only a note, so a caller can tell
            # a degraded search from a complete one without reading prose.
            {"count": len(hits), "top_score": hits[0].score, "semantic": not note},
        )

    async def fetch(args: dict, principal: Principal) -> ToolResult:
        document_id = str(args.get("id", "")).strip()
        document = index.get(principal, document_id) if document_id else None
        if document is None:
            return ToolResult.failure(f"no document {document_id!r} available to you")

        return ToolResult.success(
            f"{document.title}\n\n{document.body}",
            {"source": document.source, "url": document.url},
        )

    source.register(
        "search",
        search,
        description="Search documents the user is permitted to read.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": f"1-{MAX_RESULTS}."},
            },
            "required": ["query"],
        },
        risk=RiskClass.READ,
        # Documents are written by people, including outside parties whose
        # attachments and shared files end up indexed.
        returns_untrusted_content=True,
        identified=True,
    )
    source.register(
        "fetch",
        fetch,
        description="Read one document in full by its id.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
        identified=True,
    )
    return source
