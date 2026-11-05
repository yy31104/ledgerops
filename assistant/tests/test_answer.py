from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.embeddings import HashingEmbedder
from ledgerops_assistant.providers.base import Draft, parse_draft
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever, VectorRetriever


class Scripted:
    name = "scripted"

    def __init__(self, draft):
        self.result, self.calls = draft, 0

    def draft(self, question, passages):
        self.calls += 1
        return self.result


def test_citations_of_passages_not_given_are_dropped(tiny_corpus):
    retriever = BM25Retriever(tiny_corpus)
    real = retriever.search("timeout hold", 1)[0].chunk.chunk_id
    provider = Scripted(Draft("A timeout keeps the hold.", [real, "docs/MADE-UP.md#x/0"]))
    result = answer("Does a timeout release the hold?", retriever, provider, top_n=1)
    assert not result.refused
    assert [c.chunk_id for c in result.citations] == [real]
    assert result.dropped_citations == ["docs/MADE-UP.md#x/0"]


def test_an_answer_with_no_valid_citation_becomes_a_refusal(tiny_corpus):
    provider = Scripted(Draft("Confident but unsupported.", ["docs/MADE-UP.md#x/0"]))
    result = answer("timeout hold", BM25Retriever(tiny_corpus), provider)
    assert result.refused and result.reason == "ungrounded"
    assert "Confident" not in result.text


def test_action_requests_never_reach_the_model(tiny_corpus):
    provider = Scripted(Draft("Released!", []))
    result = answer("Release the hold on payout 7", BM25Retriever(tiny_corpus), provider)
    assert result.reason == "action_not_permitted" and provider.calls == 0


def test_live_state_questions_point_to_the_api(tiny_corpus):
    provider = Scripted(Draft("It was paid.", []))
    result = answer("Is my payout still on hold?", BM25Retriever(tiny_corpus), provider)
    assert result.reason == "needs_live_state" and provider.calls == 0
    assert "GET /v1/payouts/{id}" in result.text


def test_scope_gate_refuses_before_the_model(tiny_corpus):
    vector = VectorRetriever(tiny_corpus, HashingEmbedder())
    provider = Scripted(Draft("Pierogi need flour.", []))
    result = answer(
        "pierogi recipe", BM25Retriever(tiny_corpus), provider, gate=ScopeGate(vector, 0.5)
    )
    assert result.reason == "not_in_docs" and provider.calls == 0


def test_fake_provider_is_deterministic_and_cites_what_it_copies(tiny_corpus):
    retriever = BM25Retriever(tiny_corpus)
    first = answer("Does a timeout release the hold?", retriever, FakeProvider())
    second = answer("Does a timeout release the hold?", retriever, FakeProvider())
    assert first.text == second.text and "timeout never releases" in first.text
    assert first.citations[0].section_id == "docs/PAYOUTS.md#dispatch-and-recovery"


def test_malformed_model_json_is_a_refusal_not_an_answer():
    assert parse_draft("not json").refused
    assert parse_draft('{"answer": "x", "citations": [], "refused": false}').refused
    bad_reason = '{"answer":"x","citations":[],"refused":false,"refusal_reason":"because"}'
    assert parse_draft(bad_reason).refused
    good = parse_draft('{"answer":"x","citations":["a"],"refused":false,"refusal_reason":"none"}')
    assert not good.refused and good.citations == ["a"]
