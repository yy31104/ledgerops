from types import SimpleNamespace

import pytest

from ledgerops_assistant.policy import default_retrieval
from ledgerops_assistant.settings import Settings


def test_release_policy_names_a_supported_method():
    assert default_retrieval() in {"vector", "hybrid"}
    assert Settings().retrieval == default_retrieval()


@pytest.mark.parametrize("method", ["vector", "hybrid"])
def test_service_retrieval_can_be_selected_explicitly(monkeypatch, method):
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_RETRIEVAL", method)
    assert Settings.environment().retrieval == method


def test_unknown_retrieval_method_fails_at_startup(monkeypatch):
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_RETRIEVAL", "unknown")
    with pytest.raises(ValueError, match="retrieval"):
        Settings.environment()


@pytest.mark.parametrize("method,expected", [("vector", ["vector"]), ("hybrid", ["vector", "fts"])])
def test_runtime_builds_the_selected_read_only_retriever(
    monkeypatch, tiny_corpus, method, expected
):
    from ledgerops_assistant import runtime

    called = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            return SimpleNamespace(fetchone=lambda: ("source-revision",))

    def retriever(*args, method):
        called.append(method)
        return SimpleNamespace(name=method, search=lambda query, k: [])

    monkeypatch.setattr(runtime, "load_corpus", lambda: tiny_corpus)
    monkeypatch.setattr(runtime, "encoder", lambda settings: SimpleNamespace(name="test"))
    monkeypatch.setattr(runtime, "corpus_key", lambda *args: "corpus-id")
    monkeypatch.setattr(runtime.psycopg, "connect", lambda *args, **kwargs: Connection())
    monkeypatch.setattr(runtime, "PostgresRetriever", retriever)
    context = runtime.retriever_for(Settings(database_url="synthetic-assistant", retrieval=method))
    assert called == expected
    assert context.revision == "source-revision"
    assert context.search("question", 5) == []
