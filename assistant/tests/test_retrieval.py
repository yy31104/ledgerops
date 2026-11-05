import pytest

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.embeddings import HashingEmbedder
from ledgerops_assistant.retrieval import (
    BM25Retriever,
    Hit,
    HybridRetriever,
    VectorRetriever,
    rank_sections,
    tokenize,
)


def test_tokenize_keeps_identifiers_and_their_parts():
    assert tokenize("Why LIMIT_EXCEEDED?") == ["limit_exceeded", "limit", "exceeded"]
    assert "the" not in tokenize("the hold")


def test_bm25_prefers_the_chunk_with_the_rare_term(tiny_corpus):
    hits = BM25Retriever(tiny_corpus).search("limit exceeded", 3)
    assert hits[0].chunk.section_id == "docs/PAYOUTS.md#amounts"


def test_bm25_returns_nothing_for_unknown_words(tiny_corpus):
    assert BM25Retriever(tiny_corpus).search("pierogi recipe") == []


def test_vector_scores_are_cosine_similarities(tiny_corpus):
    hits = VectorRetriever(tiny_corpus, HashingEmbedder()).search("timeout hold lookup", 3)
    assert hits[0].chunk.section_id == "docs/PAYOUTS.md#dispatch-and-recovery"
    assert all(-1.0 <= h.score <= 1.0 + 1e-6 for h in hits)


class Fixed:
    def __init__(self, name, chunks):
        self.name, self.chunks = name, chunks

    def search(self, query, k=10):
        return [Hit(c, 1.0) for c in self.chunks[:k]]


def _chunk(cid):
    return Chunk(f"d.md#{cid}/0", f"d.md#{cid}", "d.md", "D", cid, cid)


def test_rrf_rewards_agreement_over_one_first_place():
    a, b, c = _chunk("a"), _chunk("b"), _chunk("c")
    fused = HybridRetriever(Fixed("x", [a, b, c]), Fixed("y", [c, b, a]), rrf_k=60)
    hits = fused.search("q", 3)
    # b is second in both lists: 2/62 beats a's 1/61 + 1/63 only narrowly, so check exact math.
    scores = {h.chunk.section_id: round(h.score, 6) for h in hits}
    assert scores["d.md#a"] == round(1 / 61 + 1 / 63, 6)
    assert scores["d.md#b"] == round(2 / 62, 6)


def test_rank_sections_keeps_first_occurrence_only():
    hits = [Hit(_chunk("a"), 3), Hit(_chunk("b"), 2), Hit(_chunk("a"), 1)]
    assert rank_sections(hits) == ["d.md#a", "d.md#b"]


def test_deep_weak_agreement_no_longer_displaces_a_single_list_first_place():
    semantic, weak = _chunk("semantic"), _chunk("weak")
    keyword = [_chunk(f"lexical-{i}") for i in range(50)]
    vector = [semantic, *[_chunk(f"semantic-{i}") for i in range(49)]]
    keyword[29], vector[29] = weak, weak
    retrievers = Fixed("fts", keyword), Fixed("vector", vector)
    old = [h.chunk.chunk_id for h in HybridRetriever(*retrievers, rrf_k=60).search("q", 100)]
    new = [h.chunk.chunk_id for h in HybridRetriever(*retrievers).search("q", 100)]
    assert old.index(weak.chunk_id) < old.index(semantic.chunk_id)
    assert new.index(semantic.chunk_id) < new.index(weak.chunk_id)


def test_section_votes_are_not_multiplied_by_the_number_of_chunks():
    a, b = _chunk("a"), _chunk("b")
    second = Chunk("d.md#a/1", a.section_id, a.path, a.title, a.heading, "another passage")
    fused = HybridRetriever(
        Fixed("fts", [a, second, b]),
        Fixed("vector", [b, second, a]),
        unit="section",
        weights=(0.5, 1.0),
    ).search("q", 3)
    assert len(fused) == 2
    assert len({h.chunk.section_id for h in fused}) == 2
    chosen = next(h.chunk for h in fused if h.chunk.section_id == a.section_id)
    assert chosen.chunk_id == second.chunk_id


@pytest.mark.parametrize("weights", [(1,), (0, 1), (float("nan"), 1), (-1, 1)])
def test_invalid_fusion_weights_are_rejected(weights):
    with pytest.raises(ValueError, match="configuration"):
        HybridRetriever(Fixed("a", []), Fixed("b", []), weights=weights)
