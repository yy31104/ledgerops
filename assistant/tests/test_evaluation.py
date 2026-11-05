import json

import pytest

from ledgerops_assistant import evaluation
from ledgerops_assistant.evaluation import DEFAULT_QUESTIONS, load_questions, score_ranking


def test_ranking_metrics():
    scores = score_ranking(["x", "a", "y", "b"], ["a", "b"])
    assert scores["hit@1"] == 0.0 and scores["hit@3"] == 1.0
    assert scores["recall@5"] == 1.0 and scores["mrr@10"] == 0.5


def test_report_distinguishes_unique_sections_from_five_runtime_chunks():
    from ledgerops_assistant.corpus import Chunk
    from ledgerops_assistant.retrieval import Hit

    class Repeated:
        def search(self, query, k):
            return [
                Hit(Chunk(f"d#a/{i}", "d#a", "d", "D", "a", "unrelated"), 1) for i in range(5)
            ] + [Hit(Chunk("d#b/0", "d#b", "d", "D", "b", "answer"), 1)]

    result = evaluation.evaluate_retriever(Repeated(), [_row(relevant_sections=["d#b"])])
    assert result["all"]["recall@5"] == 1
    assert result["all"]["context_recall@5"] == 0


def test_committed_questions_are_valid_against_the_real_corpus(corpus):
    questions = load_questions(DEFAULT_QUESTIONS, corpus.section_ids())
    assert len(questions) >= 60
    assert {q["expected_route"] for q in questions} == {
        "docs",
        "live_state",
        "action",
        "out_of_scope",
    }


def _write(tmp_path, rows):
    path = tmp_path / "q.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _row(**changes):
    return {
        "id": "q1",
        "group": "g",
        "split": "dev",
        "category": "rule",
        "scenario": None,
        "question": "?",
        "expected_route": "docs",
        "relevant_sections": ["d#a"],
        "key_facts": [],
        **changes,
    }


def test_paraphrase_groups_cannot_span_dev_and_test(tmp_path):
    path = _write(tmp_path, [_row(), _row(id="q2", split="test")])
    with pytest.raises(ValueError, match="groups split"):
        load_questions(path, {"d#a"})


def test_labels_must_name_existing_sections(tmp_path):
    with pytest.raises(ValueError, match="missing from the corpus"):
        load_questions(_write(tmp_path, [_row(relevant_sections=["d#gone"])]), {"d#a"})


def test_full_run_with_hashing_embedder_and_fake_provider(tmp_path):
    out = tmp_path / "report.json"
    assert (
        evaluation.main(
            [
                "--embedder",
                "hashing",
                "--backend",
                "memory",
                "--output",
                str(out),
                "--tracking-dir",
                str(tmp_path / "mlflow"),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text())
    assert set(report["retrieval"]) == {"bm25", "vector", "legacy_hybrid", "hybrid"}
    assert report["answers"]["refusal"]["wrong_refusals"] >= 0
    assert report["questions"]["retrieval_scored"] == sum(
        1
        for line in DEFAULT_QUESTIONS.read_text().splitlines()
        if json.loads(line)["expected_route"] == "docs" and json.loads(line)["split"] == "dev"
    )
