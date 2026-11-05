"""Three ways to rank chunks for a question: keyword (BM25), vector, and both fused.

Every retriever returns chunks best first, with a score whose meaning depends on the
method. Scores are only comparable within one method.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.embeddings import Embedder

_STOPWORDS = frozenset(
    "a an and are as at be been but by can could do does for from had has have how i if in "
    "into is it its may me my of on or our should so than that the their then there these "
    "this to was we were what when where which while who why will with would you your".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, no stemming.

    An identifier such as LIMIT_EXCEEDED yields both ``limit_exceeded`` and its parts,
    so "limit exceeded" written in prose can still match it.
    """
    tokens = []
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        if token in _STOPWORDS:
            continue
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if part and part not in _STOPWORDS)
    return tokens


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class Retriever(Protocol):
    name: str

    def search(self, query: str, k: int = 10) -> list[Hit]: ...


class BM25Retriever:
    """Okapi BM25 over the chunk text.

    For each query term, a chunk scores higher when the term is frequent in the chunk
    (with diminishing returns set by k1), rare across the corpus (IDF), and the chunk
    is not unusually long (length normalization set by b).
    """

    name = "bm25"

    def __init__(self, corpus: Corpus, *, k1: float = 1.2, b: float = 0.75):
        self.chunks = corpus.chunks
        self.k1, self.b = k1, b
        self.term_counts = [Counter(tokenize(chunk.text)) for chunk in self.chunks]
        self.lengths = [sum(counts.values()) for counts in self.term_counts]
        self.average_length = sum(self.lengths) / len(self.lengths)
        document_frequency = Counter(term for counts in self.term_counts for term in counts)
        n = len(self.chunks)
        self.idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for term, df in document_frequency.items()
        }

    def score(self, query_terms: list[str], index: int) -> float:
        counts, length = self.term_counts[index], self.lengths[index]
        total = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            norm = self.k1 * (1 - self.b + self.b * length / self.average_length)
            total += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
        return total

    def search(self, query: str, k: int = 10) -> list[Hit]:
        terms = tokenize(query)
        scored = [(self.score(terms, i), i) for i in range(len(self.chunks))]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [Hit(self.chunks[i], score) for score, i in scored[:k] if score > 0]


class VectorRetriever:
    """Exact nearest neighbours: cosine similarity against every chunk.

    With a few hundred chunks a full matrix-vector product takes well under a
    millisecond, so an approximate index such as HNSW would add error and buy nothing.
    """

    name = "vector"

    def __init__(self, corpus: Corpus, embedder: Embedder, *, cache_dir: Path | None = None):
        self.chunks = corpus.chunks
        self.embedder = embedder
        self.matrix = self._load_or_embed(corpus, cache_dir)

    def _load_or_embed(self, corpus: Corpus, cache_dir: Path | None) -> np.ndarray:
        texts = [chunk.text for chunk in self.chunks]
        if cache_dir is None:
            return self.embedder.embed_passages(texts)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", self.embedder.name)
        path = cache_dir / f"{safe_name}-{corpus.digest[:16]}.npy"
        if path.exists():
            matrix = np.load(path)
            if matrix.shape[0] == len(texts):
                return matrix
        matrix = self.embedder.embed_passages(texts)
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(path, matrix)
        return matrix

    def search(self, query: str, k: int = 10) -> list[Hit]:
        similarities = self.matrix @ self.embedder.embed_query(query)
        order = np.argsort(-similarities, kind="stable")[:k]
        return [Hit(self.chunks[i], float(similarities[i])) for i in order]


class HybridRetriever:
    """Reciprocal rank fusion: score = sum over methods of 1 / (k + rank).

    RRF uses ranks, not raw scores, so BM25's unbounded scores and cosine similarities
    never need to be put on one scale. The original paper selected k=60 on its own
    pilot data. Here k=10 was selected on the frozen v1 development questions;
    the larger constant over-promoted weak agreement in two deep candidate lists.
    Keep k=60 explicit when reproducing the historical baseline.
    """

    name = "hybrid"

    def __init__(
        self,
        *retrievers: Retriever,
        rrf_k: int = 10,
        depth: int = 50,
        weights: tuple[float, ...] | None = None,
        unit: str = "chunk",
    ):
        self.retrievers, self.rrf_k, self.depth = retrievers, rrf_k, depth
        self.weights = weights if weights is not None else (1.0,) * len(retrievers)
        if (
            len(self.weights) != len(retrievers)
            or not self.weights
            or any(not math.isfinite(w) or w <= 0 for w in self.weights)
            or rrf_k < 0
            or not 1 <= depth <= 100
            or unit not in ("chunk", "section")
        ):
            raise ValueError("invalid fusion configuration")
        self.unit = unit

    def search(self, query: str, k: int = 10) -> list[Hit]:
        return fuse_rankings(
            [r.search(query, self.depth) for r in self.retrievers],
            k=k,
            rrf_k=self.rrf_k,
            weights=self.weights,
            unit=self.unit,
        )


def fuse_rankings(rankings, *, k, rrf_k, weights, unit):
    """Fuse chunks or first occurrences of sections; return a supplied passage.

    Section voting counts each section once per retriever. Its representative is
    the chunk with the largest individual weighted vote (chunk ID breaks ties).
    No text is fabricated or joined across citation IDs.
    """
    fused, representatives = {}, {}
    for hits, weight in zip(rankings, weights, strict=True):
        seen = set()
        for hit in hits:
            key = hit.chunk.section_id if unit == "section" else hit.chunk.chunk_id
            if key in seen:
                continue
            seen.add(key)
            vote = weight / (rrf_k + len(seen))
            fused[key] = fused.get(key, 0.0) + vote
            choice = (-vote, hit.chunk.chunk_id)
            if key not in representatives or choice < representatives[key][0]:
                representatives[key] = (choice, hit.chunk)
    order = sorted(fused, key=lambda key: (-fused[key], key))[:k]
    return [Hit(representatives[key][1], fused[key]) for key in order]


def rank_sections(hits: list[Hit]) -> list[str]:
    """Section IDs in first-hit order, without repeats."""
    seen, sections = set(), []
    for hit in hits:
        if hit.chunk.section_id not in seen:
            seen.add(hit.chunk.section_id)
            sections.append(hit.chunk.section_id)
    return sections
