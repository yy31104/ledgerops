"""Lexical alignment between the question and the supplied evidence spans."""

import math
import re
from functools import lru_cache
from itertools import pairwise
from typing import NamedTuple

from ledgerops_assistant.coverage import STOPWORDS, stem, tokens

_CLAUSE = re.compile(r"[,;:]|\b(?:but|although|whereas)\b", re.I)
_YES_NO = re.compile(
    r"^(?:is|are|does|do|did|can|could|would|will|should|has|have|may|must)\b", re.I
)
_NEGATION = re.compile(r"\b(?:not|no|never|cannot|doesn't|does not|isn't|is not|without)\b", re.I)


class RelationFeatures(NamedTuple):
    local_focus: float
    question_body_coverage: float
    question_pair_specificity: float
    question_span_coverage: float
    inquiry_span_coverage: float
    last_terms_coverage: float
    final_clause_coverage: float
    inquiry_pair_alignment: float
    paired_question_final_clause: float
    specific_pair_inquiry: float
    specific_body_inquiry: float
    negated_inquiry: float


# Ridge logistic fit on the consumed development rows. The offline fit script
# produces raw-space weights in RelationFeatures order.
INTERCEPT = -5.658121514416359
_WITNESS_ODDS_MULTIPLIER = math.exp(0.5)
WEIGHTS = RelationFeatures(
    local_focus=5.610682390117223,
    question_body_coverage=2.803424413636381,
    question_pair_specificity=1.438343176180732,
    question_span_coverage=2.754474894643833,
    inquiry_span_coverage=7.029481287921298,
    last_terms_coverage=-3.7016411706485015,
    final_clause_coverage=3.764220212202325,
    inquiry_pair_alignment=2.2354909707779345,
    paired_question_final_clause=0.3274429470890017,
    specific_pair_inquiry=2.9059158407063648,
    specific_body_inquiry=3.735807325590645,
    negated_inquiry=-1.7519812598775173,
)


def _sequence(text):
    return tuple(
        stem(token.lower())
        for token in tokens(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    )


_passage_sequence = lru_cache(maxsize=4096)(_sequence)


def _near_pairs(sequence):
    return {
        (first, second)
        for position, first in enumerate(sequence)
        for second in sequence[position + 1 : position + 3]
        if first != second
    }


def _coverage(weights, words):
    return sum(weight for term, weight in weights.items() if term in words)


def _normalize(weights):
    total = sum(weights.values()) or 1.0
    return {term: weight / total for term, weight in weights.items()}


def passage_relations(coverage, query, inquiry, passages):
    """Measure each supplied chunk independently and identify a focal prose span."""
    inquiry_sequence = _sequence(inquiry)
    final_clause = next(
        (clause.strip() for clause in reversed(_CLAUSE.split(inquiry)) if clause.strip()), inquiry
    )
    question = {word: coverage.idf(word) for word in sorted(set(_sequence(query)))}
    question_weights = _normalize(question)
    specific_question = _normalize({word: idf**2 for word, idf in question.items()})
    inquiry_weights = _normalize({word: question[word] for word in sorted(set(inquiry_sequence))})
    last_terms = _normalize({word: question[word] for word in sorted(set(inquiry_sequence[-5:]))})
    clause = _normalize({word: question[word] for word in sorted(set(_sequence(final_clause)))})
    inquiry_pairs = _normalize(
        {(a, b): (question[a] + question[b]) / 2 for a, b in sorted(_near_pairs(inquiry_sequence))}
    )
    yes_no = bool(_YES_NO.match(inquiry))

    required_overlap = min(2, len(question))
    required_focus_overlap = min(2, len(inquiry_weights))
    for body, spans, local_focus in passages:
        spans = tuple((text, _passage_sequence(text), words) for text, words in spans)
        body_coverage = _coverage(specific_question, set(_passage_sequence(body)))
        adjacent = [(left[1] + right[1], left[2] | right[2]) for left, right in pairwise(spans)]
        if len(spans) == 1:
            adjacent = [(spans[0][1], spans[0][2])]
        paired_question = pair_specific = pair_alignment = 0.0
        for sequence, words in adjacent:
            paired_question = max(paired_question, _coverage(question_weights, words))
            pair_specific = max(pair_specific, _coverage(specific_question, words))
            if inquiry_pairs:
                pairs = _near_pairs(sequence)
                aligned = sum(
                    weight * (1.0 if (a, b) in pairs else 0.75 if (b, a) in pairs else 0.0)
                    for (a, b), weight in inquiry_pairs.items()
                )
                pair_alignment = max(pair_alignment, aligned)

        question_span = inquiry_span = last_span = clause_span = negated_span = 0.0
        best_witness_score, witness = 0.0, None
        for text, _, words in spans:
            context = _coverage(question_weights, words)
            question_span = max(question_span, context)
            focus = _coverage(inquiry_weights, words)
            inquiry_span = max(inquiry_span, focus)
            last_span = max(last_span, _coverage(last_terms, words))
            clause_span = max(clause_span, _coverage(clause, words))
            if yes_no and _NEGATION.search(text):
                negated_span = max(negated_span, focus)
            if (
                focus * context > best_witness_score
                and len(question.keys() & words) >= required_overlap
                and len(inquiry_weights.keys() & words) >= required_focus_overlap
            ):
                best_witness_score, witness = focus * context, text

        chunk = RelationFeatures(
            local_focus,
            body_coverage,
            pair_specific,
            question_span,
            inquiry_span,
            last_span,
            clause_span,
            pair_alignment,
            math.sqrt(paired_question * clause_span),
            math.sqrt(pair_specific * inquiry_span),
            math.sqrt(body_coverage * inquiry_span),
            negated_span,
        )
        yield chunk, witness


def merge_relation_features(left, right):
    return RelationFeatures(*(max(a, b) for a, b in zip(left, right, strict=True)))


def relation_score(features, *, intercept=INTERCEPT, weights=WEIGHTS):
    logit = intercept + sum(weight * value for weight, value in zip(weights, features, strict=True))
    return 1.0 / (1.0 + math.exp(-logit))


def cap_witness_score(witness, bundle):
    """A bundle can veto the single-chunk score. Evidence cannot come from the bundle."""
    witness = round(witness, 8)
    boosted = _WITNESS_ODDS_MULTIPLIER * witness / (1 + (_WITNESS_ODDS_MULTIPLIER - 1) * witness)
    return min(boosted, bundle)
