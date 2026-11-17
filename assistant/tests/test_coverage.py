"""The coverage check uses only the indexed corpus; development data sets its threshold."""

import json

import pytest

from ledgerops_assistant import routing
from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.coverage import (
    Coverage,
    DocumentCoverage,
    calibrate,
    is_code,
    stem,
    tokens,
)
from ledgerops_assistant.evaluation import DEFAULT_QUESTIONS
from ledgerops_assistant.policy import coverage_threshold
from scripts.scope_dev import pool


@pytest.mark.parametrize(
    "word,expected",
    [
        ("caresses", "caress"),
        ("ponies", "poni"),
        ("motoring", "motor"),
        ("hopping", "hop"),
        ("relational", "relat"),
        ("confirmations", "confirm"),
        ("restoration", "restor"),
        ("generalizations", "gener"),
        ("validate", "valid"),
        ("validation", "valid"),
    ],
)
def test_porter_stems(word, expected):
    assert stem(word) == expected


def test_codes_and_identifiers():
    assert [t for t in tokens("Is 3DS or PSD2 like FastAPI, v1 or IBAN?") if is_code(t)] == [
        "3DS",
        "PSD2",
        "FastAPI",
        "IBAN",
    ]
    assert tokens("payout 40000000-0000-4000-8000-000000000003 MAY_HAVE_SENT") == [
        "payout",
        "MAY",
        "HAVE",
        "SENT",
    ]


def test_unknown_codes_and_absent_terms_are_not_covered(tiny_corpus):
    coverage = DocumentCoverage(tiny_corpus, 0.6)
    assert coverage.covers("Does a timeout release the hold?")
    assert coverage.covers("Could you explain what LOOKUP does after timeouts?")
    unknown = coverage.measure("Does LOOKUP use the IBAN checksum?")
    assert unknown.unknown_codes == ("IBAN",) and not coverage.covers("Does LOOKUP use IBAN?")
    question = "How many blockchain confirmations finalize a withdrawal?"
    assert coverage.measure(question).uncovered_share == 1.0
    assert not coverage.covers(question)


def test_absent_terms_are_weighted_by_corpus_specificity(tiny_corpus):
    coverage = DocumentCoverage(tiny_corpus, 0.6)
    measured = coverage.measure("Does a timeout release the hold during blockchain settlement?")
    assert measured.missing_terms == ("blockchain", "settlement")
    assert 0 < measured.uncovered_share < 1


def test_threshold_must_be_a_share():
    with pytest.raises(ValueError, match="coverage threshold"):
        DocumentCoverage(None, 0)


def test_calibration_never_refuses_development_documentation():
    rows = [
        (True, Coverage((), (), 0.2)),
        (True, Coverage((), (), 0.5)),
        (False, Coverage((), (), 0.4)),
        (False, Coverage((), (), 0.7)),
        (False, Coverage(("IBAN",), (), 0.9)),
    ]
    result = calibrate(rows)
    assert result["threshold"] == 0.6 and result["documentation_max_share"] == 0.5


def test_uncovered_documentation_route_refuses_before_retrieval_or_model(tiny_corpus):
    class Forbidden:
        name = "must-not-be-called"

        def search(self, *args):
            raise AssertionError("uncovered question reached retrieval")

        def draft(self, *args):
            raise AssertionError("uncovered question reached a model")

    gate = ScopeGate(None, None, coverage=DocumentCoverage(tiny_corpus, 0.6))
    result = answer(
        "How do loyalty points accrue for cardholders?", Forbidden(), Forbidden(), gate=gate
    )
    assert result.refused and result.reason == "not_in_docs"
    assert result.route == routing.OUT_OF_SCOPE
    live = answer("Is my payout still on hold?", Forbidden(), Forbidden(), gate=gate)
    assert live.reason == "needs_live_state"


def test_similarity_gate_needs_both_retriever_and_threshold(tiny_corpus):
    with pytest.raises(ValueError, match="both"):
        ScopeGate(None, 0.5)


def test_release_threshold_refuses_no_development_documentation_question(corpus):
    coverage = DocumentCoverage(corpus, coverage_threshold())
    rows = pool(corpus.section_ids())
    docs = [r for r in rows if r["expected_route"] == routing.DOCS]
    assert len(docs) == 193
    assert [r["id"] for r in docs if not coverage.covers(r["question"])] == []
    outside = [r for r in rows if r["expected_route"] == routing.OUT_OF_SCOPE]
    passed = [
        r["id"]
        for r in outside
        if routing.classify(r["question"]) == routing.DOCS
        and not routing.unsupported_topic(r["question"])
        and coverage.covers(r["question"])
    ]
    assert len(outside) == 37 and passed == ["v1-053", "v3-068"]


def test_current_development_file_marks_the_v1_007_source_gap():
    rows = {json.loads(line)["id"]: json.loads(line) for line in DEFAULT_QUESTIONS.open()}
    label = rows["v1-007"]
    assert label["label_version"] == 2
    assert label["key_facts"][1] == "A new attempt needs a new operation ID"
    assert "Idempotency-Key" in label["source_gap"]
