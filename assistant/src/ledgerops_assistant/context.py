"""Bounded source context after ranking; original chunks remain individually citable."""

import json
from dataclasses import asdict

from ledgerops_assistant.corpus import Corpus

CONTEXT_POLICY = "section-neighbors-v1"
MAX_CONTEXT_BYTES = 18000


class ContextRetriever:
    """Keep search unchanged; expand five distinct sections for generation only.

    Section introductions restore conditions omitted from later split chunks.
    File neighbors may cross a heading, but never a document or corpus snapshot.
    All additions retain their original text, metadata and citation IDs.
    """

    def __init__(self, retriever, corpus: Corpus, *, max_bytes=MAX_CONTEXT_BYTES):
        if type(max_bytes) is not int or not 2 <= max_bytes <= 64000:
            raise ValueError("invalid context byte budget")
        self.retriever, self.max_bytes = retriever, max_bytes
        self.name = retriever.name
        self.revision = getattr(retriever, "revision", corpus.revision)
        self.by_id = corpus.by_id()
        self.leads, paths = {}, {}
        for chunk in corpus.chunks:
            self.leads.setdefault(chunk.section_id, chunk)
            paths.setdefault(chunk.path, []).append(chunk)
        self.positions = {
            chunk.chunk_id: (chunks, index)
            for chunks in paths.values()
            for index, chunk in enumerate(chunks)
        }

    def search(self, query, k=10):
        return self.retriever.search(query, k)

    def context(self, query, k=5):
        if not 1 <= k <= 30:
            raise ValueError("context requires 1 to 30 sections")
        seeds, sections = [], set()
        for hit in self.search(query, 30):
            if hit.chunk.section_id not in sections:
                seeds.append(hit.chunk)
                sections.add(hit.chunk.section_id)
            if len(seeds) == k:
                break
        candidates = list(seeds)
        # Do not combine a returned chunk with local text from another snapshot.
        known = [c for c in seeds if self.by_id.get(c.chunk_id) == c]
        candidates.extend(self.leads[c.section_id] for c in known)
        for distance in (1, 2):
            for chunk in known:
                chunks, index = self.positions[chunk.chunk_id]
                for neighbor in (index + distance, index - distance):
                    if 0 <= neighbor < len(chunks):
                        candidates.append(chunks[neighbor])
        selected, seen, size = [], set(), 2  # JSON list brackets.
        for chunk in candidates:
            if chunk.chunk_id in seen:
                continue
            cost = len(json.dumps(asdict(chunk)).encode()) + (2 if selected else 0)
            if size + cost <= self.max_bytes:
                selected.append(chunk)
                seen.add(chunk.chunk_id)
                size += cost
        return selected


def retrieve_passages(retriever, query, k=5):
    context = getattr(retriever, "context", None)
    return context(query, k) if context else [h.chunk for h in retriever.search(query, k)]
