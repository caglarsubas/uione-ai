"""Semantic retrieval, and the fusion that decides what wins.

The interesting tests here are not "does cosine work". They are the three ways a
hybrid search quietly gets worse than the lexical one it replaced: by leaking
through the ranker that does not filter, by letting a uniformly-bad ranking vote,
and by failing closed when the GPU is busy.
"""

from __future__ import annotations

import pytest

from uione.knowledge import (
    MIN_SIMILARITY,
    AccessControl,
    Document,
    DocumentIndex,
    Embedder,
    HybridSearch,
    VectorIndex,
    content_hash,
    cosine,
    fuse,
)
from uione.mcphub import Principal

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
CEO = Principal(user_id="ceo", roles=frozenset({"exec"}))


def document(doc_id: str, title: str, body: str, acl: AccessControl | None = None) -> Document:
    return Document(
        id=doc_id,
        title=title,
        body=body,
        source="wiki",
        acl=acl or AccessControl.organisation_wide(),
    )


class StubEmbedder:
    """Deterministic vectors, so fusion can be tested without a GPU.

    Each document gets a vector positioned by hand, which is the only way to
    write an assertion about ranking that will still mean the same thing after
    somebody swaps the embedding model.
    """

    def __init__(self, vectors: dict[str, list[float]], *, fail: bool = False) -> None:
        self.vectors = vectors
        self.fail = fail
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("engine busy")
        out = []
        for text in texts:
            for key, vector in self.vectors.items():
                if key in text:
                    out.append(vector)
                    break
            else:
                out.append([0.0, 0.0, 1.0])
        return out


# -- cosine ----------------------------------------------------------------


def test_identical_vectors_are_maximally_similar() -> None:
    assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_orthogonal_vectors_are_unrelated() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_a_zero_vector_is_similar_to_nothing() -> None:
    """An embedder can return one for empty or unrepresentable input, and
    dividing by its norm would fail the whole query."""
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_mismatched_dimensions_do_not_raise() -> None:
    """A model change mid-corpus produces these. One bad row must not take down
    a query."""
    assert cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


# -- the content hash ------------------------------------------------------


def test_editing_a_document_changes_its_hash() -> None:
    before = content_hash(document("d", "Title", "Body"))
    after = content_hash(document("d", "Title", "Body, revised"))

    assert before != after


def test_changing_permissions_does_not_change_the_hash() -> None:
    """ACLs change far more often than text. Re-embedding a corpus because
    somebody joined a group would make permission updates cost GPU time."""
    public = content_hash(document("d", "T", "B", AccessControl.organisation_wide()))
    private = content_hash(document("d", "T", "B", AccessControl.for_users("alice")))

    assert public == private


# -- fusion ----------------------------------------------------------------


def test_a_document_found_by_only_one_method_still_surfaces() -> None:
    """The reason for running both."""
    assert "semantic-only" in fuse(["a", "b"], ["semantic-only"], limit=5)


def test_two_mediocre_votes_do_not_beat_one_excellent_vote() -> None:
    """The failure that showed up on the third query ever run against this.

    Asked "when can I take time off", BM25 matched *time* and *off* in a refund
    runbook; the holiday policy shared no terms at all. The embedder ranked the
    holiday policy first and the refund runbook a distant fourth. Fused with
    equal weights the refund runbook appeared in both lists and won, and the
    document that answered the question came third.
    """
    lexical = ["refunds", "runbook"]
    semantic = ["holiday"]  # the rest fell below the similarity floor

    assert fuse(lexical, semantic, limit=3)[0] == "holiday"


def test_fusion_is_stable_between_identical_queries() -> None:
    first = fuse(["a", "b"], ["b", "c"], limit=3)
    second = fuse(["a", "b"], ["b", "c"], limit=3)

    assert first == second


def test_a_document_ranked_well_by_both_beats_one_ranked_well_by_neither() -> None:
    order = fuse(["both", "lexical-tail"], ["both", "semantic-tail"], limit=3)

    assert order[0] == "both"


# -- the similarity floor --------------------------------------------------


def test_documents_below_the_floor_are_not_returned() -> None:
    """ "The four least dissimilar documents in the corpus" is not a search
    result, and in a fusion it becomes votes for documents nothing found
    relevant."""
    vectors = VectorIndex(model="stub")
    vectors.put("near", [1.0, 0.0, 0.0], digest="a")
    vectors.put("far", [0.0, 1.0, 0.0], digest="b")

    hits = vectors.search([1.0, 0.0, 0.0], allowed={"near", "far"}, limit=10)

    assert [h.document_id for h in hits] == ["near"]


def test_the_floor_can_be_relaxed_for_a_deployment() -> None:
    vectors = VectorIndex(model="stub")
    vectors.put("far", [0.0, 1.0, 0.0], digest="b")

    assert vectors.search([1.0, 0.0, 0.0], allowed={"far"}, limit=10, floor=-1.0)


def test_the_default_floor_is_conservative() -> None:
    """Too high loses recall the lexical half covers; too low readmits the noise
    the floor exists to exclude."""
    assert 0.2 <= MIN_SIMILARITY <= 0.5


# -- permissions -----------------------------------------------------------


def test_the_vector_index_only_ranks_what_the_caller_may_read() -> None:
    """Not an optimisation. Scoring the whole corpus and filtering afterwards
    makes result counts and response times depend on documents the caller
    cannot see."""
    vectors = VectorIndex(model="stub")
    vectors.put("public", [1.0, 0.0], digest="a")
    vectors.put("secret", [1.0, 0.0], digest="b")

    hits = vectors.search([1.0, 0.0], allowed={"public"}, limit=10)

    assert [h.document_id for h in hits] == ["public"]


async def test_a_restricted_document_never_reaches_a_hybrid_result() -> None:
    index = DocumentIndex()
    index.add(document("open", "Settlement runbook", "The batch halts on a soft decline."))
    index.add(
        document(
            "secret",
            "Executive compensation",
            "Board pay review.",
            AccessControl.for_users("ceo"),
        )
    )
    model = StubEmbedder({"Executive": [1.0, 0.0, 0.0], "Settlement": [0.9, 0.1, 0.0]})
    vectors = VectorIndex(model="stub")
    await Embedder(model, vectors).sync([index.get(CEO, "open"), index.get(CEO, "secret")])

    result = await HybridSearch(index, vectors, model=model).search(ALICE, "Executive pay")

    assert "secret" not in [hit.document.id for hit in result.hits]


# -- degradation -----------------------------------------------------------


async def test_a_busy_engine_falls_back_to_lexical_results() -> None:
    """An assistant whose search stops working because a GPU is busy is worse
    than one that occasionally misses a synonym."""
    index = DocumentIndex()
    index.add(document("d", "Settlement runbook", "The batch halts."))
    vectors = VectorIndex(model="stub")
    vectors.put("d", [1.0, 0.0], digest=content_hash(index.get(ALICE, "d")))

    result = await HybridSearch(index, vectors, model=StubEmbedder({}, fail=True)).search(
        ALICE, "settlement"
    )

    assert [hit.document.id for hit in result.hits] == ["d"]
    assert result.semantic is False
    assert "keyword results" in result.note


async def test_a_deployment_with_no_vectors_still_searches() -> None:
    index = DocumentIndex()
    index.add(document("d", "Settlement runbook", "The batch halts."))

    result = await HybridSearch(index, VectorIndex(), model=StubEmbedder({})).search(
        ALICE, "settlement"
    )

    assert [hit.document.id for hit in result.hits] == ["d"]
    assert result.semantic is False


async def test_degradation_is_a_field_not_only_a_note() -> None:
    """So a caller can tell a degraded search from a complete one without
    reading prose."""
    result = await HybridSearch(DocumentIndex(), VectorIndex(), model=None).search(ALICE, "x")

    assert result.semantic is False


# -- embedding -------------------------------------------------------------


async def test_only_missing_or_stale_documents_are_embedded() -> None:
    model = StubEmbedder({"Runbook": [1.0, 0.0]})
    vectors = VectorIndex(model="stub")
    embedder = Embedder(model, vectors)
    doc = document("d", "Runbook", "Body")

    await embedder.sync([doc])
    calls_after_first = model.calls
    await embedder.sync([doc])

    assert model.calls == calls_after_first, "an unchanged document must not be re-embedded"


async def test_an_edited_document_is_re_embedded() -> None:
    model = StubEmbedder({"Runbook": [1.0, 0.0]})
    vectors = VectorIndex(model="stub")
    embedder = Embedder(model, vectors)

    await embedder.sync([document("d", "Runbook", "First")])
    before = model.calls
    await embedder.sync([document("d", "Runbook", "Second")])

    assert model.calls > before


async def test_a_failed_batch_loses_the_batch_not_the_sync() -> None:
    """Those documents stay lexically searchable and are retried next time.
    Losing a document because the embedder was busy would be worse than missing
    a synonym."""
    vectors = VectorIndex(model="stub")

    embedded = await Embedder(StubEmbedder({}, fail=True), vectors).sync(
        [document("d", "Runbook", "Body")]
    )

    assert embedded == 0
    assert len(vectors) == 0


# -- persistence -----------------------------------------------------------


async def test_vectors_survive_a_restart(tmp_path) -> None:
    from uione.config import Settings
    from uione.storage import Database, EmbeddingStore

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'v.db'}"))
    await db.create_schema()
    try:
        store = EmbeddingStore(db)
        vectors = VectorIndex(model="stub")
        await Embedder(StubEmbedder({"Runbook": [1.0, 0.0]}), vectors, store=store).sync(
            [document("d", "Runbook", "Body")]
        )

        restored = VectorIndex(model="stub")
        count = await store.load_into(restored, model="stub")

        assert count == 1
        assert restored.get("d") == [1.0, 0.0]
    finally:
        await db.dispose()


async def test_vectors_from_another_model_are_not_loaded(tmp_path) -> None:
    """Mixing embedding spaces produces similarities that are arithmetic on
    unrelated numbers — plausible, ordered, and meaningless."""
    from uione.config import Settings
    from uione.storage import Database, EmbeddingStore

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'v.db'}"))
    await db.create_schema()
    try:
        store = EmbeddingStore(db)
        await store.save("d", [1.0, 0.0], model="old-model", digest="x")

        restored = VectorIndex(model="new-model")
        count = await store.load_into(restored, model="new-model")

        assert count == 0
    finally:
        await db.dispose()


async def test_switching_model_purges_the_old_vectors(tmp_path) -> None:
    """An operator who switches embedding model would otherwise carry the old
    corpus's vectors forever, invisible and consuming space nobody can account
    for."""
    from uione.config import Settings
    from uione.storage import Database, EmbeddingStore

    db = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'v.db'}"))
    await db.create_schema()
    try:
        store = EmbeddingStore(db)
        await store.save("d", [1.0, 0.0], model="old-model", digest="x")
        await store.save("e", [0.0, 1.0], model="new-model", digest="y")

        purged = await store.purge_other_models("new-model")

        assert purged == 1
        kept = VectorIndex(model="new-model")
        assert await store.load_into(kept, model="new-model") == 1
    finally:
        await db.dispose()
