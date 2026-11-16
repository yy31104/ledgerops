"""Bounded source context after ranking; original chunks remain individually citable."""

import json
import re
from dataclasses import asdict
from itertools import zip_longest

from ledgerops_assistant.corpus import Corpus

CONTEXT_POLICY = "section-completion-v2"
MAX_CONTEXT_BYTES = 18000
_IDENTIFIERS = re.compile(r"\b(?:[A-Z][A-Z0-9]*_[A-Z0-9_]+|[A-Z]{3,})\b")


class ContextRetriever:
    """Keep search unchanged; pack source evidence after five ranked section anchors.

    Keep ranked anchors, then complete their source sections before adding file
    neighbors. Completion takes one chunk per section per round so a long command
    reference cannot consume every remaining byte. Hybrid context also keeps the vector
    component's leading sections: a single-source hit need not win the fusion vote.
    Search rankings, source text and citation IDs are unchanged.
    """

    def __init__(self, retriever, corpus: Corpus, *, max_bytes=MAX_CONTEXT_BYTES):
        if type(max_bytes) is not int or not 2 <= max_bytes <= 64000:
            raise ValueError("invalid context byte budget")
        self.retriever, self.max_bytes = retriever, max_bytes
        self.name = retriever.name
        self.revision = getattr(retriever, "revision", corpus.revision)
        self.by_id = corpus.by_id()
        self.sections, paths = {}, {}
        for chunk in corpus.chunks:
            self.sections.setdefault(chunk.section_id, []).append(chunk)
            paths.setdefault(chunk.path, []).append(chunk)
        self.positions = {
            chunk.chunk_id: (chunks, index)
            for chunks in paths.values()
            for index, chunk in enumerate(chunks)
        }

    @staticmethod
    def _cost(chunk):
        return len(json.dumps(asdict(chunk)).encode())

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
        # Fusion may bury a strong vector-only section below the first five.
        # Recover only current-snapshot component anchors, never arbitrary local text.
        supplemental = set()
        for component in getattr(self.retriever, "retrievers", ()):
            if getattr(component, "method", None) != "vector" and component.name != "vector":
                continue
            component_sections = set()
            for hit in component.search(query, 30):
                chunk = hit.chunk
                if chunk.section_id not in component_sections:
                    component_sections.add(chunk.section_id)
                    if chunk.section_id not in sections and self.by_id.get(chunk.chunk_id) == chunk:
                        seeds.append(chunk)
                        sections.add(chunk.section_id)
                        supplemental.add(chunk.section_id)
                if len(component_sections) == k:
                    break
        candidates = list(seeds)
        # Do not combine a returned chunk with local text from another snapshot.
        known = [c for c in seeds if self.by_id.get(c.chunk_id) == c]
        completions = [
            (
                c.section_id not in supplemental,
                [part for part in self.sections[c.section_id] if part.chunk_id != c.chunk_id],
            )
            for c in known
        ]
        # Complete recovered single-source sections before spending the rest on
        # sections already favored by fusion. Round-robin within each group keeps
        # a long command reference from starving another section's conditions.
        # Compact sections go first within a round; equal sizes retain rank order.
        for primary in (False, True):
            group = [parts for is_primary, parts in completions if is_primary == primary]
            group.sort(key=lambda parts: sum(self._cost(part) for part in parts))
            candidates.extend(
                part
                for round_parts in zip_longest(*group)
                for part in round_parts
                if part is not None
            )
        for distance in (1, 2):
            for chunk in known:
                chunks, index = self.positions[chunk.chunk_id]
                for neighbor in (index + distance, index - distance):
                    if 0 <= neighbor < len(chunks):
                        candidates.append(chunks[neighbor])
        # Keep an explicit code conjunction together when a nearby paragraph
        # carries it. A generic label in one anchor must not crowd out the rule
        # that relates several named states. Select at most one anchor per code;
        # ties keep candidate order and no source text is rewritten.
        remaining = set(_IDENTIFIERS.findall(query))
        code_anchors = []
        fitting = (
            [c for c in candidates if self._cost(c) + 2 <= self.max_bytes] if remaining else []
        )
        while remaining:
            best = max(
                fitting,
                key=lambda c: len(remaining.intersection(_IDENTIFIERS.findall(c.body))),
                default=None,
            )
            covered = remaining.intersection(_IDENTIFIERS.findall(best.body)) if best else set()
            if not covered:
                break
            code_anchors.append(best)
            remaining -= covered
        candidates = code_anchors + candidates
        selected, seen, size = [], set(), 2  # JSON list brackets.
        for chunk in candidates:
            if chunk.chunk_id in seen:
                continue
            cost = self._cost(chunk) + (2 if selected else 0)
            if size + cost <= self.max_bytes:
                selected.append(chunk)
                seen.add(chunk.chunk_id)
                size += cost
        return selected


def retrieve_passages(retriever, query, k=5):
    context = getattr(retriever, "context", None)
    return context(query, k) if context else [h.chunk for h in retriever.search(query, k)]
