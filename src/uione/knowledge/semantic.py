"""Semantic retrieval, and how it combines with the lexical index.

BM25 finds documents that share words with the query. It cannot find the
settlement runbook when somebody asks about "batch payments not completing",
because those documents share no terms at all — and that is the query people
actually type.

**The permission invariant does not change.** Filter first, rank second. A vector
search that ranks the whole corpus and then removes what the caller may not read
leaks existence through result counts and through timing, which is the same leak
the lexical index was carefully built to avoid. Everything here operates on the
readable subset the caller already has.

**Ranks are fused, not scores.** A BM25 score and a cosine similarity live on
different scales with different distributions, and any weighted sum of them
needs a normalisation that depends on the corpus — which changes with every
document added. Reciprocal rank fusion needs only the ordering, so it cannot be
miscalibrated. It is also the reason a document ranked well by *either* method
surfaces, which is the whole point of running both.

**Two mediocre votes must not beat one excellent vote.** Plain RRF has a failure
mode that showed up on the third query ever run against it. Asked "when can I
take time off", BM25 matched the words *time* and *off* in a refund runbook and
scored it 0.72; the holiday policy shared no terms and scored nothing. The
embedder ranked the holiday policy first at 0.49 and the refund runbook a distant
0.23. Fused naively, the refund runbook appeared in *both* lists and won, and the
document that answered the question came third.

The fix is not a weight pulled out of the air, it is an asymmetry that already
exists between the two signals. After the floor below, a semantic vote means
"similar above a stated threshold". A lexical vote means only "shares a term",
with no relevance threshold at all — BM25 returns whatever matched, however
weakly. Those two statements are not equally informative, so weighting them
equally is the arbitrary choice, not weighting them differently.

**Cosine has a floor; BM25 does not.** This is why a threshold is legitimate here
and would not be on the lexical side. For a fixed embedding model, cosine
similarity is comparable across queries and corpora — 0.2 means unrelated
whatever was asked. A BM25 score is relative to the query's length and the
corpus's term statistics, so no constant means anything. The floor is therefore
applied only where it can be defended.

**Embeddings are stored, and postings are not.** That looks inconsistent next to
the index, which deliberately rebuilds its postings at startup. The difference is
cost: a posting list is a tokeniser pass over text already in memory, while an
embedding is a round trip to a GPU per document. So embeddings are cached — keyed
by *both* the content hash and the model name, so editing a document or changing
the model produces a miss rather than a confidently wrong vector.

**Semantic search never takes search offline.** If the model plane is unreachable
or slow, the query falls back to lexical results and says so. An assistant whose
search stops working because a GPU is busy is worse than one that occasionally
misses a synonym.

**The scale this is honest about.** Similarity is brute force: every stored vector
is compared with the query. That is linear in corpus size, and fine for the tens
of thousands of documents an on-premise department has. It is not an
approximate-nearest-neighbour index and does not pretend to be; a deployment with
millions of documents needs one, and this module is where it would go.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import structlog

from uione.knowledge.documents import Document
from uione.knowledge.index import DocumentIndex, SearchHit
from uione.mcphub import Principal

log = structlog.get_logger(__name__)

#: RRF's smoothing constant. 60 is the value from the original paper and the one
#: every implementation uses; it flattens the contribution of top ranks enough
#: that a single method cannot dominate the fused order.
RRF_K = 60

#: Documents embedded per request. One request per document is one GPU round
#: trip per document, which turns a 500-document sync into 500 sequential waits.
BATCH_SIZE = 32

#: Cosine similarity below which a document is not a neighbour, just the rest of
#: the corpus. Legitimate as a constant because cosine is comparable across
#: queries for a fixed model — see the module docstring. Conservative on purpose:
#: too high loses recall, which the lexical half covers; too low readmits the
#: noise this exists to exclude.
MIN_SIMILARITY = 0.35

#: How much more a semantic vote counts than a lexical one. Not a tuning knob
#: found by experiment — it encodes that a semantic vote passed a relevance
#: threshold and a lexical vote did not.
SEMANTIC_WEIGHT = 1.5

#: How much text of a document is embedded. Most embedding models truncate
#: anyway; doing it here makes the cost predictable and the content hash stable.
MAX_EMBED_CHARS = 2000


def content_hash(document: Document) -> str:
    """Identity of the *text*, so an edit invalidates the cached vector.

    Title and body only. A permission change must not invalidate an embedding —
    ACLs change far more often than text, and re-embedding a corpus because
    somebody joined a group would make permission updates cost GPU time.
    """
    payload = f"{document.title}\n{document.body}"[:MAX_EMBED_CHARS]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def embed_text(document: Document) -> str:
    return f"{document.title}\n{document.body}"[:MAX_EMBED_CHARS]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, defensive about degenerate vectors.

    A zero vector — which an embedder can return for empty or unrepresentable
    input — has no direction, so it is not similar to anything. Returning 0
    rather than dividing by zero keeps one bad document from failing a query.
    """
    if len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


@dataclass
class SemanticHit:
    document_id: str
    score: float


@dataclass
class HybridResult:
    """What a hybrid search returns, including what it could not do."""

    hits: list[SearchHit] = field(default_factory=list)

    #: True when the semantic half ran. False means these are lexical results
    #: only — surfaced as a field so a caller can say so rather than implying
    #: a completeness it does not have.
    semantic: bool = True
    note: str = ""

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)


class VectorIndex:
    """Vectors for documents, in memory, backed by an optional store."""

    def __init__(self, *, model: str = "") -> None:
        self._vectors: dict[str, list[float]] = {}
        self._hashes: dict[str, str] = {}
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def __len__(self) -> int:
        return len(self._vectors)

    def put(self, document_id: str, vector: list[float], *, digest: str) -> None:
        self._vectors[document_id] = vector
        self._hashes[document_id] = digest

    def get(self, document_id: str) -> list[float] | None:
        return self._vectors.get(document_id)

    def remove(self, document_id: str) -> None:
        self._vectors.pop(document_id, None)
        self._hashes.pop(document_id, None)

    def is_current(self, document: Document) -> bool:
        """Whether the stored vector was made from this exact text."""
        return self._hashes.get(document.id) == content_hash(document)

    def search(
        self,
        query_vector: list[float],
        *,
        allowed: set[str],
        limit: int = 10,
        floor: float = MIN_SIMILARITY,
    ) -> list[SemanticHit]:
        """Nearest documents, considering only ones the caller may read.

        `allowed` is not an optimisation. Scoring the whole corpus and filtering
        afterwards would make result counts and response times depend on
        documents the caller cannot see.

        Results below `floor` are dropped rather than returned as weak matches.
        "The four least dissimilar documents in the corpus" is not a search
        result, and in a fusion it becomes votes for documents nothing found
        relevant.
        """
        scored = [
            SemanticHit(document_id=doc_id, score=cosine(query_vector, vector))
            for doc_id, vector in self._vectors.items()
            if doc_id in allowed
        ]
        scored = [hit for hit in scored if hit.score >= floor]
        scored.sort(key=lambda h: (-h.score, h.document_id))
        return scored[:limit]


def fuse(
    lexical: list[str],
    semantic: list[str],
    *,
    k: int = RRF_K,
    limit: int = 10,
    semantic_weight: float = SEMANTIC_WEIGHT,
) -> list[str]:
    """Weighted reciprocal rank fusion of two orderings.

    Each list contributes ``weight / (k + rank)`` per document. No score
    normalisation, because there is nothing to normalise — only positions. A
    document found by one method and missed by the other still surfaces, which
    is the reason for running both.

    The semantic list is expected to have been filtered by similarity already;
    its weight reflects that its votes cleared a threshold and the lexical
    list's did not.
    """
    scores: dict[str, float] = {}
    for ranking, weight in ((lexical, 1.0), (semantic, semantic_weight)):
        for rank, document_id in enumerate(ranking, start=1):
            scores[document_id] = scores.get(document_id, 0.0) + weight / (k + rank)

    # Ties broken by id so a fused ordering is stable between identical queries.
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [document_id for document_id, _ in ordered[:limit]]


class HybridSearch:
    """Lexical and semantic retrieval, fused, degrading to lexical alone."""

    def __init__(
        self,
        index: DocumentIndex,
        vectors: VectorIndex,
        *,
        model=None,
        store=None,
    ) -> None:
        self._index = index
        self._vectors = vectors
        self._model = model
        self._store = store

    @property
    def enabled(self) -> bool:
        return self._model is not None and len(self._vectors) > 0

    async def search(self, principal: Principal, query: str, *, limit: int = 5) -> HybridResult:
        # Lexical first and unconditionally: it is local, fast, and it is what
        # the caller gets if anything below fails.
        lexical = self._index.search(principal, query, limit=max(limit * 2, 10))

        if not self.enabled:
            return HybridResult(
                hits=lexical[:limit],
                semantic=False,
                note="semantic search unavailable; these are keyword results",
            )

        try:
            query_vector = (await self._model.embed([query]))[0]
        except Exception as exc:  # noqa: BLE001 — a busy GPU is not an outage
            log.warning("semantic.query_embed_failed", error=type(exc).__name__)
            return HybridResult(
                hits=lexical[:limit],
                semantic=False,
                note="semantic search unavailable; these are keyword results",
            )

        readable = self._index.readable_ids(principal)
        semantic = self._vectors.search(query_vector, allowed=readable, limit=max(limit * 2, 10))

        order = fuse(
            [hit.document.id for hit in lexical],
            [hit.document_id for hit in semantic],
            limit=limit,
        )

        # Rendered from the lexical hit where there is one, so snippets keep
        # highlighting the query's own words; a semantic-only match gets a
        # snippet built the same way, from the document itself.
        by_id = {hit.document.id: hit for hit in lexical}
        semantic_scores = {hit.document_id: hit.score for hit in semantic}

        hits: list[SearchHit] = []
        for document_id in order:
            if existing := by_id.get(document_id):
                hits.append(existing)
                continue
            document = self._index.get(principal, document_id)
            if document is None:  # pragma: no cover — filtered above, belt and braces
                continue
            hits.append(
                SearchHit(
                    document=document,
                    score=round(semantic_scores.get(document_id, 0.0), 4),
                    snippet=document.snippet(query),
                )
            )

        return HybridResult(hits=hits, semantic=True)


class Embedder:
    """Keeps the vector index in step with the document index."""

    def __init__(self, model, vectors: VectorIndex, *, store=None) -> None:
        self._model = model
        self._vectors = vectors
        self._store = store

    async def load(self) -> int:
        """Restore cached vectors for the current model.

        Vectors made by a different model are not loaded. Mixing embedding
        spaces produces similarities that are arithmetic on unrelated numbers —
        plausible, ordered, and meaningless.
        """
        if self._store is None:
            return 0
        restored = await self._store.load_into(self._vectors, model=self._vectors.model)
        log.info("semantic.vectors_loaded", count=restored, model=self._vectors.model)
        return restored

    async def sync(self, documents: list[Document]) -> int:
        """Embed whatever is missing or stale. Returns how many were embedded."""
        pending = [d for d in documents if not self._vectors.is_current(d)]
        if not pending:
            return 0

        embedded = 0
        for start in range(0, len(pending), BATCH_SIZE):
            batch = pending[start : start + BATCH_SIZE]
            try:
                vectors = await self._model.embed([embed_text(d) for d in batch])
            except Exception as exc:  # noqa: BLE001
                # The batch is lost, not the sync. Those documents stay
                # lexically searchable and are retried next time — losing a
                # document because the embedder was busy would be worse than
                # missing a synonym.
                log.warning("semantic.batch_failed", size=len(batch), error=type(exc).__name__)
                continue

            for document, vector in zip(batch, vectors, strict=False):
                digest = content_hash(document)
                self._vectors.put(document.id, vector, digest=digest)
                if self._store is not None:
                    await self._store.save(
                        document.id, vector, model=self._vectors.model, digest=digest
                    )
                embedded += 1

        log.info("semantic.embedded", count=embedded, pending=len(pending))
        return embedded
