import json
from types import SimpleNamespace

import pytest

from ledgerops_assistant.answer import ScopeGate
from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.support import PassageSupport, _question_parts
from ledgerops_assistant.support_relation import relation_score, witness_score
from ledgerops_assistant.support_veto import (
    CONDITIONAL_WITNESS_INTERCEPT,
    CONDITIONAL_WITNESS_WEIGHTS,
    VETO_WITNESS_INTERCEPT,
    VETO_WITNESS_WEIGHTS,
    has_fronted_or_coordinated_inquiry,
)


def passage(body, index=1, *, heading="Details"):
    return Chunk(
        f"test#details/{index}", "test#details", "test.md", "Test", heading, f"Test\n\n{body}"
    )


def corpus_for(*chunks):
    return SimpleNamespace(chunks=chunks)


def test_corpus_veto_ignores_link_targets_and_fenced_text():
    proof = passage(
        "Two receipts survive reboot. [Read the note](internal/directory).\n\n"
        "```text\npassword signature\n```",
        heading="Receipts",
    )
    corpus = corpus_for(proof, passage("Many cases use retries.", 2))
    scorer = PassageSupport(DocumentCoverage(corpus, 0.597), mode="compact-veto", corpus=corpus)
    question = "How many receipts survive reboot?"
    bundle, _ = scorer.relation_features(question, [proof])
    result = scorer.veto.features(bundle, _question_parts(question)[1], [proof])

    assert result.rare_inquiry_prose_known == pytest.approx(1.0)
    assert result.quantity_inquiry_span_coverage == pytest.approx(0.75)
    assert {"intern", "directori", "password", "signatur"}.isdisjoint(scorer.veto.source_frequency)


@pytest.mark.parametrize(
    ("mode", "intercept", "weights"),
    [
        ("compact-veto", VETO_WITNESS_INTERCEPT, VETO_WITNESS_WEIGHTS),
        ("conditional-veto", CONDITIONAL_WITNESS_INTERCEPT, CONDITIONAL_WITNESS_WEIGHTS),
    ],
)
def test_plain_prose_witness_remains_independent_from_the_bundle(mode, intercept, weights):
    receipt = passage("The receipt exists.")
    context = passage("Reserve retry limits.", 2)
    question = "Reserve retry limits. Does the receipt signature expire overnight?"
    corpus = corpus_for(receipt, context)
    scorer = PassageSupport(DocumentCoverage(corpus, 0.597), mode=mode, corpus=corpus)
    _, candidates = scorer.relation_features(question, [receipt, context])
    [(proof, features, focused)] = candidates
    score = witness_score(
        relation_score(features, intercept=intercept, weights=weights),
        focused=focused,
    )

    assert not focused
    result = scorer.measure(question, [receipt, context])
    assert result.chunk_id == proof.chunk_id == receipt.chunk_id
    assert result.witness == proof.witness == receipt.body
    assert 0 < result.score <= score
    assert scorer.measure(question, [context]).score == 0.0


@pytest.mark.parametrize("mode", ["compact-veto", "conditional-veto"])
def test_configured_veto_requires_a_procedure_witness(monkeypatch, mode):
    from ledgerops_assistant import answer

    question = "How can I cancel an expired receipt?"
    denial = passage("You cannot cancel an expired receipt.")
    proof = passage("A caller can cancel an expired receipt.", 2)
    monkeypatch.setattr(answer, "support_mode", lambda: mode)
    monkeypatch.setattr(answer, "support_threshold", lambda: 0.3)
    corpus = corpus_for(denial, proof, passage("A snapshot preserves earlier records.", 3))
    gate = ScopeGate.for_corpus(None, None, corpus)

    assert not gate.supports(question, [denial])
    assert gate.supports(question, [denial, proof])
    assert gate.support.measure(question, [denial, proof]).chunk_id == proof.chunk_id


@pytest.mark.parametrize(
    ("inquiry", "expected"),
    [
        ("If a worker restarts, how does it read receipts?", True),
        ("Does a worker record receipts, and where can a caller see the record?", True),
        ("Does a worker report a transient error?", False),
        ("Is the receipt signed, or is it?", False),
    ],
)
def test_fronted_or_coordinated_inquiry(inquiry, expected):
    assert has_fronted_or_coordinated_inquiry(inquiry) is expected


def test_conditional_runtime_coefficients_match_the_recorded_fit():
    from ledgerops_assistant import support_veto
    from scripts.evaluate_v13 import FIT_RECORDS

    fit = json.loads(FIT_RECORDS["conditional-veto"].read_text())
    assert fit["intercept"] == support_veto.CONDITIONAL_VETO_INTERCEPT
    assert fit["weights"] == support_veto.CONDITIONAL_VETO_WEIGHTS._asdict()
    assert fit["context_intercept"] == support_veto.CONDITIONAL_CONTEXT_INTERCEPT
    assert fit["context_weights"] == support_veto.CONDITIONAL_CONTEXT_WEIGHTS._asdict()
    assert fit["witness_intercept"] == support_veto.CONDITIONAL_WITNESS_INTERCEPT
    assert fit["witness_weights"] == support_veto.CONDITIONAL_WITNESS_WEIGHTS._asdict()
    assert fit["focused_witness_log_odds_shift"] == (
        support_veto.CONDITIONAL_FOCUSED_WITNESS_LOG_ODDS_SHIFT
    )
