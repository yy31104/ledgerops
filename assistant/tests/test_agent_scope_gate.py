from types import SimpleNamespace

from test_agent import client

from ledgerops_assistant.agent import Agent
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import BM25Retriever


def test_docs_gate_is_applied_before_planning_or_tools(tiny_corpus):
    seen = []

    class Gate:
        def admits(self, question):
            seen.append(question)
            return False, 0.1

    agent = Agent(BM25Retriever(tiny_corpus), FakeProvider(), scope_gate=Gate())
    state = agent.run("What are the lunar settlement rules?", client())
    assert state["result"]["reason"] == "not_in_docs"
    assert state["steps"] == 0 and state["usage"]["model_calls"] == 0
    assert seen == ["What are the lunar settlement rules?"]


def test_live_reads_do_not_depend_on_document_similarity(tiny_corpus):
    def forbidden(question):
        raise AssertionError("a live lookup must not be gated by documentation similarity")

    gate = SimpleNamespace(admits=forbidden)
    agent = Agent(BM25Retriever(tiny_corpus), FakeProvider(), scope_gate=gate)
    state = agent.run("Is payout 40000000-0000-4000-8000-000000000003 paid now?", client())
    assert state["steps"] > 0
