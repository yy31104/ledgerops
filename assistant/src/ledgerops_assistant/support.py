"""Find local lexical evidence in the documentation actually supplied to a model."""

import math
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import pairwise

from ledgerops_assistant.corpus import _FENCE
from ledgerops_assistant.coverage import stem, terms
from ledgerops_assistant.support_relation import (
    RelationFeatures,
    cap_witness_score,
    merge_relation_features,
    passage_relations,
    relation_score,
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LIST_ITEM = re.compile(r"^\s*(?:[-+*]|[0-9]+[.)])\s+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[|:\s-]+$")
_PROCEDURE = re.compile(
    r"\bhow\s+(?:to|(?:can|could|do|should|would|must)\s+(?:i|we|you|one))\s+([a-z]+)\b",
    re.I,
)
_DENIED_ACTION = re.compile(
    r"\b(?:cannot|can't|can not|does not|do not|doesn't|don't|must not|never|not allowed to)"
    r"\s+(?:be\s+)?([a-z]+)\b",
    re.I,
)
_UNAVAILABLE = re.compile(
    r"\b(?:(?:is|are) (?:not (?:supported|available|possible|allowed)|unsupported|unavailable)"
    r"|(?:do|does) not (?:offer|support|allow|provide))\b",
    re.I,
)
_CONTRAST = re.compile(r";|\bbut\b", re.I)
_INQUIRY_END = re.compile(r"(?<=[?!.;])\s+")
_PRODUCT_NAME = re.compile(r"\bLedgerOps\b", re.I)
SUPPORT_MODES = frozenset({"single-span", "local-focus", "compact-relation"})


@dataclass(frozen=True)
class Support:
    score: float = 0.0
    chunk_id: str | None = None
    witness: str | None = None


def _spans(body):
    """Keep sentences, list items and table rows separate, excluding fenced examples."""
    paragraph = []
    fenced = False
    for line in body.splitlines():
        fence = bool(_FENCE.match(line))
        stripped = line.strip()
        row = stripped.startswith("|")
        if paragraph and (fence or not stripped or row or _LIST_ITEM.match(line)):
            yield from _SENTENCE_END.split("\n".join(paragraph).strip())
            paragraph.clear()
        if fence:
            fenced = not fenced
        elif not fenced and stripped:
            if not row:
                paragraph.append(line)
            elif not _TABLE_SEPARATOR.fullmatch(line):
                yield stripped
    if paragraph:
        yield from _SENTENCE_END.split("\n".join(paragraph).strip())


@lru_cache(maxsize=256)
def _span_terms(body, procedure):
    spans = _spans(body)
    if procedure:
        spans = (clause.strip() for span in spans for clause in _CONTRAST.split(span))
    return tuple((span, frozenset(terms(span))) for span in spans if span)


def _denies_action(span, action):
    span = span.replace("\u2019", "'")
    return bool(_UNAVAILABLE.search(span)) or any(
        stem(word.lower()) == action for word in _DENIED_ACTION.findall(span)
    )


def _requested_action(question):
    procedure = _PROCEDURE.search(question)
    return stem(procedure[1].lower()) if procedure else None


def _affirmative_action(span, present, action, query_terms):
    return (
        action in present
        and any(term != action and term in present for term in query_terms)
        and not _denies_action(span, action)
    )


def _weighted_recall(weights, total, present):
    return sum(weight for term, weight in weights.items() if term in present) / total


def _question_parts(question):
    query = _PRODUCT_NAME.sub("", question).strip()
    inquiry = next(
        (part.strip() for part in reversed(_INQUIRY_END.split(query)) if part.strip()), ""
    )
    return query, inquiry


class PassageSupport:
    """Measure lexical support within the selected document evidence.

    Single-span scores one clause and discounts absent vocabulary. Local-focus
    checks the entire query against two adjacent spans and its last inquiry
    against one span of the same chunk. An explicit denial can answer a question
    about capability availability. A procedure with a known action needs an
    affirmative witness for the action and another subject term. Compact-relation
    combines span, adjacent span and query-part features fitted on development data.
    """

    def __init__(self, coverage, *, mode="single-span"):
        if mode not in SUPPORT_MODES:
            raise ValueError(f"unknown passage support mode: {mode}")
        self.coverage = coverage
        self.mode = mode

    def measure(self, question, passages):
        if self.mode == "compact-relation":
            best = self._measure_compact_relation(question, passages)
        elif self.mode == "local-focus":
            best = self._measure_local_focus(question, passages)
        else:
            best = self._measure_single_span(question, passages)
        # Quantize the final score so an identical cutoff survives platform rounding.
        return replace(best, score=round(min(best.score, 1.0), 8))

    def relation_features(self, question, passages):
        """Return a bundle for vetoing and independent positive chunk witnesses."""
        query, inquiry = _question_parts(question)
        procedure = _PROCEDURE.match(inquiry)
        action = stem(procedure[1].lower()) if procedure else None
        inquiry_terms = terms(inquiry)
        require_affirmative = action in self.coverage.frequency and action in inquiry_terms
        local = [self._measure_local_focus(question, [chunk]) for chunk in passages]
        spans = (
            (chunk.body, _span_terms(chunk.body, False), round(proof.score, 8))
            for chunk, proof in zip(passages, local, strict=True)
        )
        bundle = RelationFeatures(*([0.0] * len(RelationFeatures._fields)))
        candidates = []
        measured = passage_relations(self.coverage, query, inquiry, spans)
        for chunk, proof, (features, alternate) in zip(passages, local, measured, strict=True):
            bundle = merge_relation_features(bundle, features)
            if require_affirmative:
                if proof.witness and _affirmative_action(
                    proof.witness, terms(proof.witness), action, inquiry_terms
                ):
                    candidates.append((proof, features))
                continue
            if proof.witness:
                candidates.append((proof, features))
            elif alternate:
                candidates.append((Support(chunk_id=chunk.chunk_id, witness=alternate), features))
        return bundle, candidates

    def _measure_compact_relation(self, question, passages):
        bundle, candidates = self.relation_features(question, passages)
        winner = max(
            ((relation_score(features), witness) for witness, features in candidates),
            key=lambda candidate: candidate[0],
            default=None,
        )
        if winner is None:
            return Support()
        score, witness = winner
        return replace(witness, score=cap_witness_score(score, relation_score(bundle)))

    def _measure_single_span(self, question, passages):
        weights = {term: self.coverage.idf(term) ** 3 for term in sorted(terms(question))}
        total = sum(weights.values())
        if not total:
            return Support()
        action = _requested_action(question)
        # A generic wrapper word removed from the query is not an action constraint.
        if action not in weights:
            action = None
        discount = (1 - self.coverage.measure(question).uncovered_share) / total
        best = Support()
        for chunk in passages:
            for span, present in _span_terms(chunk.body, bool(action)):
                if action and not _affirmative_action(span, present, action, weights):
                    continue
                matched = sum(weight for term, weight in weights.items() if term in present)
                score = matched * discount
                if score > best.score:
                    best = Support(score, chunk.chunk_id, span)
        return best

    def _measure_local_focus(self, question, passages):
        query, inquiry = _question_parts(question)
        weights = {term: self.coverage.idf(term) for term in sorted(terms(query))}
        inquiry_terms = terms(inquiry)
        focus_weights = {term: weight for term, weight in weights.items() if term in inquiry_terms}
        total, focus_total = sum(weights.values()), sum(focus_weights.values())
        if not total or not focus_total:
            return Support()

        # Action wording absent from the corpus may be a paraphrase. Apply the
        # affirmative-clause constraint only for actions the corpus actually uses.
        action = _requested_action(query)
        if action not in weights or action not in self.coverage.frequency:
            action = None

        best = Support()
        for chunk in passages:
            spans = _span_terms(chunk.body, False)
            if not spans:
                continue
            windows = (left[1] | right[1] for left, right in pairwise(spans))
            if len(spans) == 1:
                windows = iter((spans[0][1],))
            full_recall = max(_weighted_recall(weights, total, window) for window in windows)
            for span, present in _span_terms(chunk.body, bool(action)):
                if action and not _affirmative_action(span, present, action, weights):
                    continue
                inquiry_recall = _weighted_recall(focus_weights, focus_total, present)
                score = math.sqrt(full_recall * inquiry_recall)
                if score > best.score:
                    best = Support(score, chunk.chunk_id, span)
        return best
