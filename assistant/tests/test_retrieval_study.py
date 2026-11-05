import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def study(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("retrieval_study")


def test_validation_refuses_overwriting_an_exposed_run(study, tmp_path):
    output = tmp_path / "run.json"
    output.write_text("{}")
    with pytest.raises(ValueError, match="overwrite"):
        study.validate("", None, None, output)


@pytest.mark.parametrize(
    "relative",
    [
        "src/ledgerops_assistant/evaluation.py",
        "src/ledgerops_assistant/postgres.py",
        "src/ledgerops_assistant/embeddings.py",
        "src/ledgerops_assistant/sql/001_rag.sql",
        "scripts/retrieval_study.py",
        "uv.lock",
    ],
)
def test_changed_search_or_scoring_code_fails_before_retrieval(
    study, monkeypatch, tmp_path, relative
):
    questions = tmp_path / "questions.jsonl"
    questions.write_text("")
    freeze = tmp_path / "freeze.json"
    freeze.write_text(
        json.dumps(
            {
                "corpus_digest": "corpus",
                "embedder": "model",
                "holdout_sha256": study.sha(questions),
                "code_sha256": {relative: "changed"},
            }
        )
    )
    monkeypatch.setattr(study, "load_corpus", lambda: SimpleNamespace(digest="corpus"))
    monkeypatch.setattr(study, "BgeSmallEmbedder", lambda: SimpleNamespace(name="model"))
    with pytest.raises(ValueError, match="code changed"):
        study.validate("", questions, freeze, tmp_path / "out.json")


def test_interrupted_attempt_cannot_be_retried_with_another_output(study, monkeypatch, tmp_path):
    questions = tmp_path / "questions.jsonl"
    questions.write_text("")
    freeze = tmp_path / "freeze.json"
    freeze.write_text(
        json.dumps(
            {
                "corpus_digest": "corpus",
                "embedder": "model",
                "holdout_sha256": study.sha(questions),
                "code_sha256": {},
            }
        )
    )
    monkeypatch.setattr(
        study, "load_corpus", lambda: SimpleNamespace(digest="corpus", section_ids=lambda: set())
    )
    monkeypatch.setattr(study, "BgeSmallEmbedder", lambda: SimpleNamespace(name="model"))

    def interrupted(*args):
        raise RuntimeError("interrupted after the canonical reservation")

    monkeypatch.setattr(study, "load_questions", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        study.validate("", questions, freeze, tmp_path / "first.json")
    assert (
        json.loads(freeze.with_name("validation-attempt.json").read_text())["status"] == "started"
    )
    with pytest.raises(FileExistsError):
        study.validate("", questions, freeze, tmp_path / "second.json")


def test_cluster_comparison_also_reports_actual_context_coverage(study):
    metrics = ["recall@5", "mrr@10", "ndcg@5", "context_recall@5"]
    rows = {qid: dict.fromkeys(metrics, 0.5) for qid in ("a", "b")}
    result = study.compare(
        rows,
        rows,
        [
            {"id": "a", "group": "one"},
            {"id": "b", "group": "two"},
        ],
    )
    assert set(result) == set(metrics)
    for metric in metrics:
        assert result[metric]["mean_difference"] == 0
        assert result[metric]["ci95"] == [0, 0]
        assert result[metric]["cluster_ci95"] == [0, 0]
