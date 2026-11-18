from types import SimpleNamespace

import pytest

from ledgerops_assistant.answer import ScopeGate
from ledgerops_assistant.corpus import Chunk
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.support import PassageSupport, _question_parts
from ledgerops_assistant.support_relation import relation_score, witness_score
from ledgerops_assistant.support_veto import VETO_WITNESS_INTERCEPT, VETO_WITNESS_WEIGHTS


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


def test_plain_prose_witness_remains_independent_from_the_bundle():
    receipt = passage("The receipt exists.")
    context = passage("Reserve retry limits.", 2)
    question = "Reserve retry limits. Does the receipt signature expire overnight?"
    corpus = corpus_for(receipt, context)
    scorer = PassageSupport(DocumentCoverage(corpus, 0.597), mode="compact-veto", corpus=corpus)
    _, candidates = scorer.relation_features(question, [receipt, context])
    [(proof, features, focused)] = candidates
    score = witness_score(
        relation_score(
            features,
            intercept=VETO_WITNESS_INTERCEPT,
            weights=VETO_WITNESS_WEIGHTS,
        ),
        focused=focused,
    )

    assert not focused
    result = scorer.measure(question, [receipt, context])
    assert result.chunk_id == proof.chunk_id == receipt.chunk_id
    assert result.witness == proof.witness == receipt.body
    assert 0 < result.score <= score
    assert scorer.measure(question, [context]).score == 0.0


def test_configured_veto_requires_a_procedure_witness(monkeypatch):
    from ledgerops_assistant import answer

    question = "How can I cancel an expired receipt?"
    denial = passage("You cannot cancel an expired receipt.")
    proof = passage("A caller can cancel an expired receipt.", 2)
    monkeypatch.setattr(answer, "support_mode", lambda: "compact-veto")
    monkeypatch.setattr(answer, "support_threshold", lambda: 0.3)
    corpus = corpus_for(denial, proof, passage("A snapshot preserves earlier records.", 3))
    gate = ScopeGate.for_corpus(None, None, corpus)

    assert not gate.supports(question, [denial])
    assert gate.supports(question, [denial, proof])
    assert gate.support.measure(question, [denial, proof]).chunk_id == proof.chunk_id
