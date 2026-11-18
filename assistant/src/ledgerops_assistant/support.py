"""Find local lexical evidence in the documentation actually supplied to a model."""

import re
from dataclasses import dataclass, replace
from functools import lru_cache

from ledgerops_assistant.corpus import _FENCE
from ledgerops_assistant.coverage import stem, terms

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
def _span_terms(body):
    return tuple((span, frozenset(terms(span))) for span in _spans(body))


def _denies_action(span, action):
    span = span.replace("\u2019", "'")
    if any(stem(word.lower()) == action for word in _DENIED_ACTION.findall(span)):
        return True
    return any(
        _UNAVAILABLE.search(clause) and action in terms(clause) for clause in _CONTRAST.split(span)
    )


class PassageSupport:
    """Weighted term coverage in one source span, discounted for absent corpus terms.

    High-IDF stems carry most of the score. A direct procedure question also needs
    the requested action and a second subject term in the same span. An explicit
    denial can answer a question about whether a capability exists. It cannot
    witness a procedure for that capability.
    """

    def __init__(self, coverage):
        self.coverage = coverage

    def measure(self, question, passages):
        weights = {term: self.coverage.idf(term) ** 3 for term in sorted(terms(question))}
        total = sum(weights.values())
        if not total:
            return Support()
        procedure = _PROCEDURE.search(question)
        action = stem(procedure[1].lower()) if procedure else None
        # A generic wrapper word removed from the query is not an action constraint.
        if action not in weights:
            action = None
        discount = (1 - self.coverage.measure(question).uncovered_share) / total
        best = Support()
        for chunk in passages:
            for span, present in _span_terms(chunk.body):
                if action and (
                    action not in present
                    or not any(term != action and term in present for term in weights)
                    or _denies_action(span, action)
                ):
                    continue
                matched = sum(weight for term, weight in weights.items() if term in present)
                score = matched * discount
                if score > best.score:
                    best = Support(score, chunk.chunk_id, span)
        # Quantize the final score so an identical cutoff survives platform rounding.
        return replace(best, score=round(min(best.score, 1.0), 8))
