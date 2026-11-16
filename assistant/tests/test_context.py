import json
from dataclasses import asdict, replace

import pytest

from ledgerops_assistant.answer import answer
from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.providers.base import Draft
from ledgerops_assistant.retrieval import Hit


def chunk(section, part=0, *, path="docs/A.md", body=None):
    return Chunk(
        f"{path}#{section}/{part}",
        f"{path}#{section}",
        path,
        "A",
        section,
        f"A > {section}\n\n" + (body or f"Source {section} part {part}."),
    )


class Ranked:
    name = "ranked"

    def __init__(self, chunks):
        self.hits = [Hit(c, 1 / (i + 1)) for i, c in enumerate(chunks)]

    def search(self, query, k=10):
        return self.hits[:k]


def test_context_diversifies_sections_and_restores_original_neighbor_citations(tmp_path):
    a, b, c, next_section, other_file = (
        chunk("rule", 0, body="The condition is in the introduction."),
        chunk("rule", 1),
        chunk("rule", 2),
        chunk("next", body="A boundary condition in the next heading."),
        chunk("private", path="docs/B.md"),
    )
    corpus = Corpus(tmp_path, "snapshot", (a, b, c, next_section, other_file), {})
    base = Ranked([c, b, a, next_section])
    expanded = ContextRetriever(base, corpus)
    assert expanded.search("rule", 2) == base.search("rule", 2)
    passages = expanded.context("rule", 2)
    assert passages[:2] == [c, next_section]
    assert a in passages and b in passages
    assert other_file not in passages
    assert len({p.chunk_id for p in passages}) == len(passages)
    assert all(p == corpus.by_id()[p.chunk_id] for p in passages)

    class Model:
        name = "scripted"

        def draft(self, question, provided):
            assert a in provided and next_section in provided
            return Draft(
                "The introduction and boundary state the conditions.",
                [a.chunk_id, next_section.chunk_id],
            )

    result = answer("Explain the rule", expanded, Model(), top_n=2)
    assert not result.refused
    assert {x.chunk_id for x in result.citations} == {a.chunk_id, next_section.chunk_id}
    assert result.retrieved == [p.chunk_id for p in passages]


def test_context_obeys_serialized_byte_budget_without_partial_chunks(tmp_path):
    first = chunk("huge", body="\u00e9" * 1000)
    second = chunk("small")
    corpus = Corpus(tmp_path, "snapshot", (first, second), {})
    budget = len(json.dumps([asdict(second)]).encode())
    context = ContextRetriever(Ranked([first, second]), corpus, max_bytes=budget).context("x")
    assert context == [second]
    assert len(json.dumps([asdict(x) for x in context]).encode()) <= budget


def test_foreign_snapshot_chunk_cannot_pull_local_neighbors(tmp_path):
    first, second = chunk("rule"), chunk("rule", 1)
    corpus = Corpus(tmp_path, "old", (first, second), {})
    foreign = replace(first, text="Different snapshot content")
    assert ContextRetriever(Ranked([foreign]), corpus).context("x") == [foreign]


@pytest.mark.parametrize("budget", [0, 1, 64001, True, 100.5])
def test_invalid_context_budget_rejected(tmp_path, budget):
    corpus = Corpus(tmp_path, "snapshot", (), {})
    with pytest.raises(ValueError):
        ContextRetriever(Ranked([]), corpus, max_bytes=budget)


@pytest.mark.postgres
def test_postgres_context_retains_immutable_snapshot_and_raw_ranking(pg_dsn, tiny_corpus):
    from ledgerops_assistant.embeddings import HashingEmbedder
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus

    encoder = HashingEmbedder(384)
    key = index_corpus(pg_dsn, tiny_corpus, encoder)
    base = PostgresRetriever(pg_dsn, key, encoder, method="vector")
    expanded = ContextRetriever(base, tiny_corpus)
    assert expanded.search("timeout", 5) == base.search("timeout", 5)
    assert all(c == tiny_corpus.by_id()[c.chunk_id] for c in expanded.context("timeout"))


@pytest.fixture(scope="module")
def development_retrievers(pg_dsn, corpus):
    from ledgerops_assistant.embeddings import BgeSmallEmbedder
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus
    from ledgerops_assistant.retrieval import HybridRetriever

    encoder = BgeSmallEmbedder()
    key = index_corpus(pg_dsn, corpus, encoder)
    vector = PostgresRetriever(pg_dsn, key, encoder, method="vector")
    return {
        "vector": vector,
        "hybrid": HybridRetriever(PostgresRetriever(pg_dsn, key, encoder, method="fts"), vector),
    }


@pytest.mark.postgres
@pytest.mark.model
@pytest.mark.parametrize("method", ["vector", "hybrid"])
@pytest.mark.parametrize(
    "question,facts",
    [
        (
            "A payout was refused for lack of funds. If the merchant has funds now, "
            "will resending the same request succeed?",
            [
                "Adding funds today does not change an earlier retained refusal.",
                "old operation ID still returns its original 422; a new operation",
            ],
        ),
        (
            "What does a FAILED or CLOSED_ABSENT result do to the hold?",
            ["to 80 / 0 / 80 with no outgoing journal"],
        ),
        (
            "What happens to a provider message with a missing or wrong signature?",
            [
                "bounded quarantine, without a financial incident",
                "An invalid signature returns 401",
            ],
        ),
        (
            "The provider reported success but local settlement stopped with LOCAL_BLOCKER. "
            "How is it resolved?",
            ["The proposer submits", "A different actor reads and approves", "Execution rechecks"],
        ),
        (
            "Which time zone defines a business day for reconciliation?",
            ["Europe/Warsaw local calendar", "23 or 25 hours"],
        ),
        (
            "Reconciliation reports MISSING_INTERNAL for a payout that is still HELD. "
            "What does that mean?",
            ["A provider payment with only a local HELD state remains MISSING_INTERNAL"],
        ),
    ],
)
def test_development_source_conditions_reach_answer_provider(
    corpus, development_retrievers, method, question, facts
):
    # This verifies evidence availability, not real-model answer correctness.
    base = development_retrievers[method]
    expanded = ContextRetriever(base, corpus)

    class Model:
        name = "scripted"

        def draft(self, question, passages):
            sources = [
                next(c for c in passages if fact in " ".join(c.text.split())) for fact in facts
            ]
            return Draft("; ".join(facts), [c.chunk_id for c in sources])

    result = answer(question, expanded, Model(), revision=corpus.revision)
    assert not result.refused and result.citations
    assert expanded.search(question, 5) == base.search(question, 5)


def test_vector_only_section_can_reach_hybrid_context_without_changing_search(tmp_path):
    original = [chunk("leading", i, body="L" * 300) for i in range(3)]
    recovered = [chunk("recovered", i, body="Condition " + str(i)) for i in range(3)]
    corpus = Corpus(tmp_path, "snapshot", tuple(original + recovered), {})
    base = Ranked([original[2]])
    component = Ranked([recovered[2]])
    component.name = "vector"
    base.retrievers = (component,)
    expected = [original[2], recovered[2], recovered[0], recovered[1]]
    budget = len(json.dumps([asdict(c) for c in expected]).encode())
    expanded = ContextRetriever(base, corpus, max_bytes=budget)
    assert expanded.search("x") == base.search("x")
    selected = expanded.context("x", 1)
    assert selected == expected
    assert len(json.dumps([asdict(c) for c in selected]).encode()) <= budget


def test_foreign_component_result_does_not_add_source_text_or_neighbors(tmp_path):
    primary = chunk("primary")
    current = chunk("secondary", path="docs/B.md")
    later = chunk("secondary", 1, path="docs/B.md")
    corpus = Corpus(tmp_path, "snapshot", (primary, current, later), {})
    base = Ranked([primary])
    component = Ranked([replace(current, text="Foreign snapshot")])
    component.name = "vector"
    base.retrievers = (component,)
    assert ContextRetriever(base, corpus).context("x") == [primary]


def test_section_completion_precedes_unrelated_file_neighbors(tmp_path):
    first = chunk("large", 0, body="Opening condition")
    middle = chunk("large", 1, body="Important consequence")
    last = chunk("large", 2, body="Terminal label")
    unrelated = chunk("other", body="Unrelated adjacent heading")
    corpus = Corpus(tmp_path, "snapshot", (first, middle, last, unrelated), {})
    budget = len(json.dumps([asdict(c) for c in (last, first, middle)]).encode())
    selected = ContextRetriever(Ranked([last]), corpus, max_bytes=budget).context("x", 1)
    assert selected == [last, first, middle]


def test_named_identifier_conjunction_survives_a_tight_budget(tmp_path):
    label = chunk("label", body="ALPHA is a named state.")
    condition = chunk("condition", body="ALPHA with BETA requires the documented condition.")
    corpus = Corpus(tmp_path, "snapshot", (label, condition), {})
    budget = len(json.dumps([asdict(condition)]).encode())
    base = Ranked([label])
    context = ContextRetriever(base, corpus, max_bytes=budget)
    assert context.search("Does ALPHA require BETA?", 1) == base.search("x", 1)
    assert context.context("Does ALPHA require BETA?", 1) == [condition]


def test_oversized_code_match_cannot_hide_a_fitting_condition(tmp_path):
    huge = chunk("large", body="ALPHA BETA " + "x" * 1000)
    condition = chunk("condition", body="ALPHA with BETA requires the documented condition.")
    unrelated = chunk("unrelated", path="docs/B.md", body="Unrelated " * 6)
    corpus = Corpus(tmp_path, "snapshot", (huge, condition, unrelated), {})
    budget = max(len(json.dumps([asdict(c)]).encode()) for c in (condition, unrelated))
    selected = ContextRetriever(Ranked([huge, unrelated]), corpus, max_bytes=budget).context(
        "Does ALPHA require BETA?", 2
    )
    assert selected == [condition]
