"""Does the indexed documentation mention what a question asks about?

Whole-question similarity cannot tell a supported question from an unsupported
one in the same domain: "How many blockchain confirmations finalize a payout?"
embeds close to payout passages because most of its words are payout words.
This check looks at the question's own terms instead:

- a code or acronym (IBAN, PSD2, 3DS) that never appears in the documentation names
  something the documentation does not describe;
- otherwise, the question's terms are weighted by how specific they are in the
  corpus (IDF over passages). A term the corpus never uses gets the largest weight.
  When the absent terms carry most of the weight, the documentation cannot answer it.

Terms are lowercased Porter stems, so "confirmations" and "confirmed" match. The
vocabulary comes only from the indexed corpus, not from a list of unsupported
topics. The share threshold is calibrated on development questions.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from ledgerops_assistant.corpus import Corpus

# Snowball's English stop-word list (as used by PostgreSQL's english configuration).
STOPWORDS = frozenset(
    """i me my myself we our ours ourselves you your yours yourself yourselves he him his
    himself she her hers herself it its itself they them their theirs themselves what which
    who whom this that these those am is are was were be been being have has had having do
    does did doing a an the and but if or because as until while of at by for with about
    against between into through during before after above below to from up down in out on
    off over under again further then once here there when where why how all any both each
    few more most other some such no nor not only own same so than too very s t can will
    just don should now""".split()
) | frozenset(
    # Modal and request words frame a question to the assistant (see the request
    # grammar in ``routing``); they say nothing about the documented subject.
    """could would might must shall may ought please kindly explain describe summarize
    summarise clarify compare define outline teach walk help understand tell show""".split()
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+[A-Za-z][A-Za-z0-9]*")


def tokens(text: str) -> list[str]:
    """Word tokens; identifiers split at underscores and hyphens, UUIDs dropped."""
    return _TOKEN.findall(_UUID.sub(" ", text).replace("_", " "))


def is_code(token: str) -> bool:
    """Acronyms and codes: two capitals (IBAN, FastAPI) or a capital with a digit (3DS)."""
    capitals = sum(ch.isupper() for ch in token)
    return capitals >= 2 or (capitals >= 1 and any(ch.isdigit() for ch in token))


def _consonant(word: str, i: int) -> bool:
    if word[i] in "aeiou":
        return False
    return word[i] != "y" or i == 0 or not _consonant(word, i - 1)


def _measure(stem: str) -> int:
    pattern = "".join("c" if _consonant(stem, i) else "v" for i in range(len(stem)))
    return re.sub("v+", "v", re.sub("c+", "c", pattern)).count("vc")


def _has_vowel(stem: str) -> bool:
    return any(not _consonant(stem, i) for i in range(len(stem)))


def _double(word: str) -> bool:
    return len(word) > 1 and word[-1] == word[-2] and _consonant(word, len(word) - 1)


def _cvc(word: str) -> bool:
    n = len(word)
    return (
        n >= 3
        and _consonant(word, n - 3)
        and not _consonant(word, n - 2)
        and _consonant(word, n - 1)
        and word[-1] not in "wxy"
    )


_STEP2 = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("abli", "able"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
)
_STEP3 = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)
_STEP4 = (
    "al",
    "ance",
    "ence",
    "er",
    "ic",
    "able",
    "ible",
    "ant",
    "ement",
    "ment",
    "ent",
    "ion",
    "ou",
    "ism",
    "ate",
    "iti",
    "ous",
    "ive",
    "ize",
)


def stem(word: str) -> str:
    """Porter (1980) stemmer for one lowercase word."""
    if len(word) <= 2:
        return word
    if word.endswith("sses") or word.endswith("ies"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if word.endswith("eed"):
        if _measure(word[:-3]) > 0:
            word = word[:-1]
    else:
        for suffix in ("ed", "ing"):
            if word.endswith(suffix) and _has_vowel(word[: -len(suffix)]):
                word = word[: -len(suffix)]
                if word.endswith(("at", "bl", "iz")):
                    word += "e"
                elif _double(word) and word[-1] not in "lsz":
                    word = word[:-1]
                elif _measure(word) == 1 and _cvc(word):
                    word += "e"
                break
    if word.endswith("y") and _has_vowel(word[:-1]):
        word = word[:-1] + "i"
    for table in (_STEP2, _STEP3):
        for suffix, replacement in table:
            if word.endswith(suffix):
                if _measure(word[: -len(suffix)]) > 0:
                    word = word[: -len(suffix)] + replacement
                break
    for suffix in _STEP4:
        if word.endswith(suffix):
            base = word[: -len(suffix)]
            if _measure(base) > 1 and (suffix != "ion" or base.endswith(("s", "t"))):
                word = base
            break
    if word.endswith("e"):
        base = word[:-1]
        if _measure(base) > 1 or (_measure(base) == 1 and not _cvc(base)):
            word = base
    if _measure(word) > 1 and _double(word) and word.endswith("l"):
        word = word[:-1]
    return word


def terms(text: str) -> set[str]:
    return {
        stem(token.lower())
        for token in tokens(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    }


@dataclass(frozen=True)
class Coverage:
    unknown_codes: tuple[str, ...]
    missing_terms: tuple[str, ...]
    uncovered_share: float


class DocumentCoverage:
    """Corpus vocabulary and passage frequencies; refuses what the sources never mention."""

    def __init__(self, corpus: Corpus, threshold: float):
        if not 0 < threshold <= 1:
            raise ValueError("coverage threshold must be in (0, 1]")
        self.threshold = threshold
        self.surface: set[str] = set()
        frequency: Counter[str] = Counter()
        for chunk in corpus.chunks:
            words = tokens(chunk.text)
            self.surface.update(word.lower() for word in words)
            frequency.update(terms(chunk.text))
        self.frequency = frequency
        self.passages = len(corpus.chunks)

    def idf(self, term: str) -> float:
        return math.log((self.passages + 1) / (self.frequency.get(term, 0) + 1))

    def measure(self, question: str) -> Coverage:
        codes = sorted(
            {t for t in tokens(question) if is_code(t) and t.lower() not in self.surface}
        )
        found = terms(question)
        missing = sorted(t for t in found if t not in self.frequency)
        total = sum(self.idf(t) for t in found)
        share = sum(self.idf(t) for t in missing) / total if total else 0.0
        return Coverage(tuple(codes), tuple(missing), round(share, 4))

    def covers(self, question: str) -> bool:
        result = self.measure(question)
        return not result.unknown_codes and result.uncovered_share < self.threshold


def calibrate(rows: list[tuple[bool, Coverage]]) -> dict:
    """Choose the share threshold on development rows of (is_documentation, coverage).

    No documentation question may be refused. The threshold sits halfway between
    the largest documentation share and the next out-of-scope share above it.
    """
    docs = [c.uncovered_share for is_docs, c in rows if is_docs and not c.unknown_codes]
    outside = [c.uncovered_share for is_docs, c in rows if not is_docs and not c.unknown_codes]
    highest = max(docs, default=0.0)
    above = [share for share in outside if share > highest]
    threshold = round((highest + min(above)) / 2, 4) if above else min(1.0, highest + 0.05)
    return {
        "threshold": threshold,
        "documentation_max_share": highest,
        "next_out_of_scope_share": min(above) if above else None,
        "documentation_with_unknown_codes": sum(
            1 for is_docs, c in rows if is_docs and c.unknown_codes
        ),
        "rule": "midpoint between the largest documentation share and the next "
        "out-of-scope share; no development documentation question is refused",
    }
