"""Corpus-derived lexical features for a documentation support veto."""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import NamedTuple

from ledgerops_assistant.coverage import STOPWORDS, stem, tokens
from ledgerops_assistant.support_relation import RelationFeatures, relation_score

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+(?:[A-Za-z][A-Za-z0-9]*)?")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_INLINE = re.compile(r"`([^`]+)`")
_QUANTITY_QUESTION = re.compile(
    r"\b(?:how (?:much|many|long|often|far|large|small|fast)|"
    r"what (?:amount|size|count|number|percentage|limit|duration|ratio|capacity|frequency|"
    r"interval|budget|rate|bound)|(?:which|what) (?:values?|numbers?))\b",
    re.I,
)
_QUANTITY = re.compile(
    r"\b(?:\d+(?:[.]\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"hundred|thousand|million)\b",
    re.I,
)


_FRONTED = re.compile(
    r"^\s*(?:if|when|while|under|after|before|in|for|once|given|assuming|during|with)\b",
    re.I,
)
_QUESTION_WORDS = r"what|which|who|whom|whose|why|where|when|how"
_AUXILIARIES = r"do|does|did|is|are|can|could|should|would|will|was|were|has|have|must|may"
_POLAR_HEADS = frozenset((*_AUXILIARIES.split("|"), "whether"))
_AUXILIARY = re.compile(rf"\b(?:{_AUXILIARIES})\b", re.I)
_COORDINATED = re.compile(
    rf"(?:[,;]\s*|\b(?:and|or|then)\s+)(?P<head>{_QUESTION_WORDS}|{_AUXILIARIES}|whether)\b",
    re.I,
)


def has_fronted_or_coordinated_inquiry(inquiry: str) -> bool:
    if _FRONTED.match(inquiry):
        return True
    for match in _COORDINATED.finditer(inquiry):
        if match["head"].lower() not in _POLAR_HEADS or _AUXILIARY.search(inquiry, match.end()):
            return True
        words = tokens(inquiry[match.start("head") :])
        if sum(len(word) > 1 and word.lower() not in STOPWORDS for word in words) >= 3:
            return True
    return False


class VetoFeatures(NamedTuple):
    local_focus: float
    question_body_coverage: float
    inquiry_span_coverage: float
    last_terms_coverage: float
    final_clause_coverage: float
    negated_inquiry: float
    common_inquiry_body_coverage: float
    rare_inquiry_prose_known: float
    rare_inquiry_window_coverage: float
    quantity_inquiry_span_coverage: float
    inquiry_pack_cohesion: float
    inquiry_span_margin: float
    inquiry_heading_coverage: float
    inquiry_prose_known: float
    inquiry_span_coherence: float


# Fitted on consumed development evidence by scripts/fit_scope_relation.py.
VETO_INTERCEPT = -6.970688452669268
VETO_WEIGHTS = VetoFeatures(
    local_focus=4.3300032876091015,
    question_body_coverage=1.5055522163055597,
    inquiry_span_coverage=3.9621152377025344,
    last_terms_coverage=-2.9817198204194786,
    final_clause_coverage=4.5791641646005745,
    negated_inquiry=-3.24783713791544,
    common_inquiry_body_coverage=4.037143875246836,
    rare_inquiry_prose_known=3.406320687971433,
    rare_inquiry_window_coverage=2.1583176066602285,
    quantity_inquiry_span_coverage=3.6807390573309027,
    inquiry_pack_cohesion=1.2870364024932694,
    inquiry_span_margin=4.4069190180681055,
    inquiry_heading_coverage=3.6313288218377786,
    inquiry_prose_known=-3.7286249306140062,
    inquiry_span_coherence=4.822744688729812,
)
VETO_WITNESS_INTERCEPT = -5.931528925923761
VETO_WITNESS_WEIGHTS = RelationFeatures(
    local_focus=5.602600226581382,
    question_body_coverage=3.4469488926282152,
    question_pair_specificity=-0.7698104893923563,
    question_span_coverage=3.0557611846313693,
    inquiry_span_coverage=7.78477998526554,
    last_terms_coverage=-3.4728485493420505,
    final_clause_coverage=4.685932762724432,
    inquiry_pair_alignment=4.554770486474177,
    paired_question_final_clause=0.3651850480228426,
    specific_pair_inquiry=1.3119038064951707,
    specific_body_inquiry=3.561141517989047,
    negated_inquiry=-2.176992236809105,
)


# The conditional fit reuses the same features and pools the contextual
# inquiry coefficients toward the overall fit.
CONDITIONAL_FOCUSED_WITNESS_LOG_ODDS_SHIFT = 2.5
CONDITIONAL_VETO_INTERCEPT = -6.942046341753068
CONDITIONAL_VETO_WEIGHTS = VetoFeatures(
    local_focus=1.974530601779362,
    question_body_coverage=1.4935509558961866,
    inquiry_span_coverage=1.6359988219889952,
    last_terms_coverage=0.17111815965102792,
    final_clause_coverage=1.8121430731898238,
    negated_inquiry=-0.4130978860963043,
    common_inquiry_body_coverage=1.8417871679185074,
    rare_inquiry_prose_known=1.1500598681880327,
    rare_inquiry_window_coverage=0.9564909306551779,
    quantity_inquiry_span_coverage=1.5995202815305305,
    inquiry_pack_cohesion=0.994436357416403,
    inquiry_span_margin=1.871455408101735,
    inquiry_heading_coverage=1.7159184848504876,
    inquiry_prose_known=1.8567831147693181,
    inquiry_span_coherence=1.2123689913757012,
)
CONDITIONAL_CONTEXT_INTERCEPT = -5.409053642712563
CONDITIONAL_CONTEXT_WEIGHTS = VetoFeatures(
    local_focus=1.913713019870838,
    question_body_coverage=1.4485035191969913,
    inquiry_span_coverage=1.560914451130211,
    last_terms_coverage=0.11875952913658169,
    final_clause_coverage=1.7952531159989669,
    negated_inquiry=-0.21565120912226635,
    common_inquiry_body_coverage=2.068760339492755,
    rare_inquiry_prose_known=1.1753233557429468,
    rare_inquiry_window_coverage=0.8858855673546979,
    quantity_inquiry_span_coverage=1.5221230437245927,
    inquiry_pack_cohesion=1.2758233064174962,
    inquiry_span_margin=1.7790226545977037,
    inquiry_heading_coverage=1.768726011501466,
    inquiry_prose_known=1.577602709297045,
    inquiry_span_coherence=1.1342630837192849,
)
CONDITIONAL_WITNESS_INTERCEPT = -6.005401354380323
CONDITIONAL_WITNESS_WEIGHTS = RelationFeatures(
    local_focus=5.4556218351818115,
    question_body_coverage=4.440986196229519,
    question_pair_specificity=-0.09187018697439109,
    question_span_coverage=3.3565253656291976,
    inquiry_span_coverage=6.141536420522036,
    last_terms_coverage=-3.0502336938176406,
    final_clause_coverage=4.811766614548429,
    inquiry_pair_alignment=5.193391913703807,
    paired_question_final_clause=-0.12335848254863124,
    specific_pair_inquiry=0.8645298068302132,
    specific_body_inquiry=3.455874461969575,
    negated_inquiry=-1.2709517701676474,
)


def conditional_veto_score(features: VetoFeatures, inquiry: str) -> float:
    if has_fronted_or_coordinated_inquiry(inquiry):
        return relation_score(
            features, intercept=CONDITIONAL_CONTEXT_INTERCEPT, weights=CONDITIONAL_CONTEXT_WEIGHTS
        )
    return relation_score(
        features, intercept=CONDITIONAL_VETO_INTERCEPT, weights=CONDITIONAL_VETO_WEIGHTS
    )


_stem = lru_cache(maxsize=8192)(stem)


def _terms(text):
    return frozenset(
        _stem(word)
        for word in _TOKEN.findall(text.lower())
        if len(word) > 1 and not word.isdigit() and word not in STOPWORDS
    )


def _clean_prose(text):
    def inline(match):
        value = match[1]
        return value if len(_TOKEN.findall(value)) <= 4 and "/" not in value else " "

    return _INLINE.sub(inline, _LINK.sub(r"\1", text))


@dataclass(frozen=True)
class _Profile:
    spans: tuple[tuple[frozenset[str], bool], ...]
    windows: tuple[frozenset[str], ...]
    body: frozenset[str]
    heading: frozenset[str]


def _profile(chunk, split_spans):
    prose = map(_clean_prose, split_spans(chunk.body))
    spans = tuple((_terms(span), bool(_QUANTITY.search(span))) for span in prose)
    words = tuple(terms for terms, _ in spans)
    windows = tuple(left | right for left, right in pairwise(words)) or words
    return _Profile(spans, windows, frozenset().union(*words), _terms(chunk.heading))


def _normalize(weights):
    total = sum(weights.values()) or 1.0
    return {term: weight / total for term, weight in weights.items()}


def _share(weights, present):
    return sum(weight for term, weight in weights.items() if term in present)


class VetoEvidence:
    """Measure how specifically the supplied prose relates to the final inquiry."""

    def __init__(self, coverage, corpus, split_spans):
        self.coverage = coverage
        self.split_spans = split_spans
        self.profiles = {chunk: _profile(chunk, split_spans) for chunk in corpus.chunks}
        terms_by_file = defaultdict(set)
        for chunk, profile in self.profiles.items():
            terms_by_file[chunk.path].update(profile.body)
        self.source_frequency = Counter(term for terms in terms_by_file.values() for term in terms)

    def features(self, relation, inquiry, passages):
        focus = _terms(inquiry)
        specificity = {term: self.coverage.idf(term) for term in sorted(focus)}
        weights = _normalize(specificity)
        rare = sorted(focus, key=lambda term: (-specificity[term], term))[:3]
        rare_weights = _normalize({term: specificity[term] for term in rare})
        breadth = _normalize({term: math.log(1 + self.source_frequency[term]) for term in weights})
        uniform = _normalize(dict.fromkeys(weights, 1))
        numeric = bool(_QUANTITY_QUESTION.search(inquiry))
        pack_words = set()
        body_matches, span_matches = [], []
        common, rare_window, quantity_match, heading, coherence = 0.0, 0.0, 0.0, 0.0, 0.0
        for chunk in passages:
            profile = self.profiles.get(chunk)
            if profile is None:
                profile = _profile(chunk, self.split_spans)
            pack_words.update(profile.body)
            body_matches.append(_share(weights, profile.body))
            common = max(common, _share(breadth, profile.body))
            window_peak = max(
                (_share(rare_weights, terms) for terms in profile.windows), default=0.0
            )
            rare_window = max(rare_window, window_peak)
            heading = max(heading, _share(weights, profile.heading))
            span_peak = 0.0
            for terms, contains_quantity in profile.spans:
                match = _share(weights, terms)
                span_peak = max(span_peak, match)
                if not numeric or contains_quantity:
                    quantity_match = max(quantity_match, match)
                coherence = max(coherence, match * len(terms & focus) / max(len(focus), 1))
            span_matches.append(span_peak)
        return VetoFeatures(
            relation.local_focus,
            relation.question_body_coverage,
            relation.inquiry_span_coverage,
            relation.last_terms_coverage,
            relation.final_clause_coverage,
            relation.negated_inquiry,
            common,
            _share(rare_weights, self.source_frequency),
            rare_window,
            quantity_match,
            max(body_matches, default=0.0) / max(0.000001, _share(weights, pack_words)),
            max(span_matches, default=0.0) - sum(span_matches) / max(len(span_matches), 1),
            heading,
            _share(uniform, self.source_frequency),
            coherence,
        )
