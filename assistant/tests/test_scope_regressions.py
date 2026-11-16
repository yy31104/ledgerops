"""Known unsupported capabilities are development regressions, not held-out scores."""

import pytest

from ledgerops_assistant import routing
from ledgerops_assistant.answer import answer


@pytest.mark.parametrize(
    "question",
    [
        "What are the PSD2 limits on low-value strong customer authentication?",
        "How long is a BLIK payment code valid?",
        "How do MCC and card-present processing change the interchange rate?",
        "Which NBP exchange-rate table converts EUR fees for tax purposes?",
        "Which exchange\u2011rate applies to tax withholding?",
        "Does the card_present channel change the interchange fee?",
    ],
)
def test_external_capability_refuses_before_retrieval_or_inference(question):
    class Forbidden:
        name = "must-not-be-called"

        def search(self, *args):
            raise AssertionError("unsupported question reached retrieval")

        def draft(self, *args):
            raise AssertionError("unsupported question reached a model")

    result = answer(question, Forbidden(), Forbidden())
    assert result.refused and result.reason == "not_in_docs"
    assert result.route == routing.OUT_OF_SCOPE


@pytest.mark.parametrize(
    "question",
    [
        "Which currency is used for payout minor units?",
        "What does a provider key identify?",
        "How do token scopes restrict an operator?",
        "Does a timeout release the hold?",
    ],
)
def test_supported_rule_terms_do_not_hit_external_capabilities(question):
    assert not routing.unsupported_topic(question)


@pytest.mark.parametrize(
    "question",
    [
        "A callback has a bad signature; does the service retain it?",
        "The old token was replaced; can its key debit twice?",
        "The worker is still running; what does freezing change?",
    ],
)
def test_scenario_questions_accept_semicolon_as_sentence_boundary(question):
    assert routing.classify(question) == routing.DOCS


@pytest.mark.parametrize(
    "question",
    [
        "The old token was replaced; release this payout hold.",
        "The worker is still running; please approve its payout.",
    ],
)
def test_semicolon_does_not_hide_an_imperative(question):
    assert routing.classify(question) == routing.ACTION
