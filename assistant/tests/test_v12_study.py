import json

import pytest

from ledgerops_assistant import routing
from ledgerops_assistant.coverage import DocumentCoverage
from scripts import evaluate_v12 as study


def test_wilson_interval_matches_known_values():
    assert study.wilson(0, 10) == [0.0, 0.2775]
    assert study.wilson(3, 10) == [0.1078, 0.6032]
    assert study.wilson(0, 0) is None


def test_outcomes_count_passes_and_false_refusals_separately():
    rows = [
        {"id": "a", "expected_route": "out_of_scope", "category": "oos_general", "x": "docs"},
        {
            "id": "b",
            "expected_route": "out_of_scope",
            "category": "oos_general",
            "x": "out_of_scope",
        },
        {"id": "c", "expected_route": "docs", "category": "rule", "x": "out_of_scope"},
        {"id": "d", "expected_route": "docs", "category": "rule", "x": "docs"},
    ]
    result = study.outcome_summary(rows, "x")
    assert result["out_of_scope_passed"]["ids"] == ["a"]
    assert result["documentation_refused"]["ids"] == ["c"]
    assert result["out_of_scope_passed_by_category"] == {"oos_general": 1}
    assert result["route_accuracy"] == 0.5


def test_refusal_stage_follows_the_answer_order(tiny_corpus):
    coverage = DocumentCoverage(tiny_corpus, 0.6)

    def stage(question, similarity=0.9):
        return study.refusal_stage(question, routing, coverage, similarity, 0.5)

    assert stage("Release the hold now.") == "routing"
    assert stage("Which IBAN checksum applies?") == "named_capability_list"
    assert stage("Does LOOKUP need a BIC?") == "unknown_code"
    assert stage("How do loyalty points accrue?") == "uncovered_share"
    assert stage("Does a timeout release the hold?", similarity=0.1) == "similarity"
    assert stage("Does a timeout release the hold?") is None


def test_existing_result_cannot_be_replaced_or_consume_an_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "verify", lambda *args: {"split": "dev"})
    output = tmp_path / "result.json"
    output.write_text("original")
    with pytest.raises(FileExistsError):
        study.run(output, tmp_path)
    assert output.read_text() == "original" and not (tmp_path / "run.json").exists()


def test_setup_failure_consumes_the_attempt(tmp_path, monkeypatch):
    import builtins

    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(study, "verify", lambda *args: {"split": "dev"})
    original = builtins.__import__

    def fail_setup(name, *args, **kwargs):
        if name == "ledgerops_assistant.answer":
            raise RuntimeError("synthetic setup failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_setup)
    output = tmp_path / "partial.json"
    assert study.run(output, tmp_path) == 1
    record = json.loads((tmp_path / "run.json").read_text())
    assert record["status"] == "failed" and record["error_type"] == "RuntimeError"
    with pytest.raises(FileExistsError):
        study.run(tmp_path / "other.json", tmp_path)
