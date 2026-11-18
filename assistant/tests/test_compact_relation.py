import math
from types import SimpleNamespace

import pytest

from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.support import PassageSupport
from ledgerops_assistant.support_relation import INTERCEPT, WEIGHTS, witness_score


def passage(body, index=1):
    return Chunk(
        f"test#details/{index}", "test#details", "test.md", "Test", "Details", f"Test\n\n{body}"
    )


def compact_scorer(*known_terms):
    coverage = SimpleNamespace(idf=lambda _: 1.0, frequency=set(known_terms))
    return PassageSupport(coverage, mode="compact-relation")


def test_compact_features_and_scored_witness():
    scorer = compact_scorer()
    question = "Connection retry. Can the receipt expire without signature?"
    chunks = [passage("A receipt expires after restart. No signature is needed.")]
    features, candidates = scorer.relation_features(question, chunks)
    [(witness, single, focused)] = candidates

    assert features == pytest.approx(
        (0.5, 0.5, 0.5, 1 / 3, 0.5, 0.5, 0.5, 0.4, 0.5, 0.5, 0.5, 0.25)
    )
    assert single == features
    assert focused
    logit = INTERCEPT + sum(weight * value for weight, value in zip(WEIGHTS, features, strict=True))
    expected = round(1 / (1 + math.exp(-logit)), 8)
    result = scorer.measure(question, chunks)
    assert result.score == expected
    assert result.chunk_id == witness.chunk_id == chunks[0].chunk_id
    assert result.witness == witness.witness == "A receipt expires after restart."


def test_fenced_text_does_not_supply_a_prose_witness():
    scorer = compact_scorer()
    question = "Can the receipt expire without signature?"
    chunks = [passage("```text\nreceipt expires without signature\n```")]
    features, candidates = scorer.relation_features(question, chunks)

    assert features.question_body_coverage == pytest.approx(1.0)
    assert features.inquiry_span_coverage == 0.0
    assert features.inquiry_pair_alignment == 0.0
    assert candidates == []
    assert scorer.measure(question, chunks).score == 0.0
    assert scorer.measure(question, []).score == 0.0


def test_availability_and_procedure_keep_distinct_negation_signals():
    scorer = compact_scorer()
    chunks = [passage("A caller cannot cancel a payout.")]
    available, _ = scorer.relation_features("Can a caller cancel a payout?", chunks)
    procedure, _ = scorer.relation_features("How can a caller cancel a payout?", chunks)

    assert available.negated_inquiry == pytest.approx(1.0)
    assert procedure.negated_inquiry == 0.0


@pytest.mark.parametrize(
    "question",
    ["How can I cancel an expired payout?", "Please explain how to cancel an expired payout?"],
)
def test_direct_procedure_requires_action_and_subject_in_one_prose_span(question):
    scorer = compact_scorer("cancel")
    action = passage("To cancel a checkout, contact an operator.")
    subject = passage("A payout expires without a response.", 2)
    denial = passage("You cannot cancel an expired payout.", 3)
    proof = passage("A caller can cancel an expired payout.", 4)

    assert scorer.measure(question, [action, subject, denial]).score == 0.0
    result = scorer.measure(question, [action, subject, denial, proof])
    assert result.chunk_id == proof.chunk_id
    assert result.witness == proof.body
    assert result.score > 0.45


def test_only_focused_witnesses_receive_a_score_increase():
    assert witness_score(0.4, focused=False) == 0.4
    assert 0.4 < witness_score(0.4, focused=True) < 1.0
    assert witness_score(0.0, focused=True) == 0.0


def test_background_passage_cannot_turn_one_inquiry_term_into_a_witness():
    scorer = compact_scorer()
    question = "Reserve retry limits. Does the receipt signature expire overnight?"
    chunks = [passage("The receipt exists."), passage("Reserve retry limits.", 2)]

    standalone = scorer.measure(question, chunks[:1])
    combined = scorer.measure(question, chunks)
    assert combined.score <= standalone.score
    assert combined.witness == standalone.witness == chunks[0].body


def test_background_procedure_does_not_replace_the_action_in_the_inquiry():
    scorer = compact_scorer("request", "cancel")
    question = "I know how to request a payout. How can I cancel a pending payout?"
    proof = passage("To cancel a pending payout, open the operator console and select Cancel.")

    result = scorer.measure(question, [proof])

    assert result.witness == proof.body
    assert result.score > 0.45
