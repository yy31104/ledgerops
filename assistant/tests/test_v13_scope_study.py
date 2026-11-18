import hashlib
import json
import math
from pathlib import Path

from scripts.evaluate_v13 import (
    PROTOCOL,
    STUDY,
    TARGET_COMMIT,
    git,
    paired_recall,
    pinned_reference,
    select_threshold,
)


def test_targets_are_the_original_preregistration():
    assert PROTOCOL.read_bytes() == git(
        "show", f"{TARGET_COMMIT}:assistant/eval/v13-scope/protocol.json"
    )


def test_frozen_trials_remain_bound_to_their_archived_inputs():
    for folder in sorted(STUDY.glob("round-*")):
        archive = json.loads((folder / "manifest.json").read_text())
        root = Path(__file__).resolve().parents[2]
        for relative, expected in archive["inputs_sha256"].items():
            assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
        for relative, expected in archive["code_sha256"].items():
            original = git("show", f"{archive['code_commit']}:{relative}")
            assert hashlib.sha256(original).hexdigest() == expected


def test_reported_trials_match_their_one_run_receipts():
    for summary_path in sorted(STUDY.glob("round-*/summary.json")):
        manifest = (summary_path.parent / "manifest.json").read_bytes()
        receipt = json.loads((summary_path.parent / "run.json").read_text())
        summary = json.loads(summary_path.read_text())
        assert receipt["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
        assert receipt["freeze_commit"] == receipt["verified_origin_head"]
        assert summary["freeze_commit"] == receipt["freeze_commit"]
        assert summary["status"] == receipt["status"]
        if receipt["status"] == "completed":
            assert summary["raw_result_sha256"] == receipt["result_sha256"]


def test_continuation_requires_a_recorded_miss_in_each_earlier_round(monkeypatch, tmp_path):
    import pytest

    from scripts import evaluate_v13 as runner

    monkeypatch.setattr(runner, "STUDY", tmp_path)
    for number in range(1, 4):
        folder = tmp_path / f"round-{number}"
        folder.mkdir()
        (folder / "summary.json").write_text('{"passed": false}\n')
    runner.verify_schedule(4)
    (tmp_path / "round-2" / "summary.json").write_text('{"passed": true}\n')
    with pytest.raises(ValueError, match="reported miss"):
        runner.verify_schedule(4)


def test_calibration_prefers_scope_reduction_within_documentation_constraint():
    docs = [
        {"id": str(i), "expected_route": "docs", "reference_admitted": True, "score": 0.8}
        for i in range(19)
    ] + [{"id": "19", "expected_route": "docs", "reference_admitted": True, "score": 0.3}]
    outside = [
        {"id": "x", "expected_route": "out_of_scope", "reference_admitted": True, "score": 0.6},
        {"id": "y", "expected_route": "out_of_scope", "reference_admitted": False, "score": 0.9},
    ]
    selected, curve = select_threshold(docs + outside)
    assert selected["threshold"] == math.nextafter(0.6, math.inf)
    assert selected["documentation_refused"]["count"] == 1
    assert selected["out_of_scope_passed"]["count"] == 0
    assert curve[0]["out_of_scope_passed"]["count"] == 1
    assert curve[-1]["documentation_refused"]["count"] == 20


def test_reference_loads_the_released_question_grammar():
    from ledgerops_assistant import routing

    question = "Under which setting does the health probe run?"
    assert pinned_reference()(question) == routing.ACTION
    assert routing.classify(question) == routing.DOCS


def test_calibration_includes_a_documentation_question_recovered_by_routing():
    rows = [
        {
            "id": "rule",
            "expected_route": "docs",
            "reference_admitted": False,
            "candidate_eligible": True,
            "score": 0.8,
        },
        {
            "id": "gap",
            "expected_route": "out_of_scope",
            "reference_admitted": True,
            "candidate_eligible": True,
            "score": 0.2,
        },
    ]
    chosen, _ = select_threshold(rows)
    assert chosen["threshold"] == math.nextafter(0.2, math.inf)
    assert chosen["documentation_refused"]["count"] == 0
    assert chosen["out_of_scope_passed"]["count"] == 0


def test_paired_recall_preserves_outcomes_inside_source_groups():
    rows = [
        {"group": "a", "reference_recall": 0.25, "candidate_recall": 0.375},
        {"group": "a", "reference_recall": 0.375, "candidate_recall": 0.5},
        {"group": "b", "reference_recall": 0.5, "candidate_recall": 0.625},
    ]
    report = paired_recall(rows, resamples=100)
    assert report["clusters"] == 2
    assert report["difference"] == 0.125
    assert report["section_cluster_ci95"] == [0.125, 0.125]


def test_release_support_cutoff_is_bound_to_a_development_record():
    from ledgerops_assistant.policy import support_mode, support_threshold

    record = json.loads((STUDY / "development.json").read_text())
    assert support_threshold() == record["support_threshold"]
    assert support_mode() == record.get("support_mode", "single-span")
    root = Path(__file__).resolve().parents[2]
    for name, digest in record["development_hashes"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_untracked_inputs_cannot_be_used_for_a_frozen_attempt(monkeypatch, tmp_path):
    import pytest

    from scripts import evaluate_v13 as runner

    def local_git(*args):
        if args == ("rev-parse", "HEAD"):
            return b"expected\n"
        if args == ("status", "--porcelain"):
            return b"?? assistant/eval/v13-scope/round-1/questions.jsonl\n"
        raise AssertionError("an untracked input must fail before remote verification")

    monkeypatch.setattr(runner, "git", local_git)
    with pytest.raises(ValueError, match="clean tracked tree"):
        runner.verify_frozen_checkout(tmp_path, "expected")
    assert not (tmp_path / "run.json").exists()


def test_failed_setup_consumes_the_frozen_attempt(monkeypatch, tmp_path):
    import pytest

    from ledgerops_assistant.policy import support_mode, support_threshold
    from scripts import evaluate_v13 as runner

    development = json.loads((STUDY / "development.json").read_text())
    threshold = support_threshold()
    trial = tmp_path / "round-1"
    trial.mkdir()
    (trial / "development.json").write_text(json.dumps(development))
    (trial / "manifest.json").write_text("{}\n")
    rows = [
        {
            "id": str(index),
            "expected_route": route,
            "split": "test",
            "group": "source#heading",
            "relevant_sections": ["source#heading"] if route == "docs" else [],
            "category": "documentation" if route == "docs" else "product_gap",
        }
        for index, route in enumerate(["docs"] * 60 + ["out_of_scope"] * 60)
    ]
    (trial / "questions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(runner, "STUDY", tmp_path)
    monkeypatch.setattr(
        runner,
        "verify_frozen_checkout",
        lambda folder, commit: (
            {"round": 1, "support_threshold": threshold, "support_mode": support_mode()},
            commit,
        ),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("database initialization failed")

    monkeypatch.setattr(runner, "measure", fail)
    result = tmp_path / "raw.json"
    with pytest.raises(RuntimeError, match="initialization"):
        runner.run(1, result, "frozen")
    assert json.loads((trial / "run.json").read_text())["status"] == "failed"
    assert json.loads(result.read_text())["status"] == "failed"
    second = tmp_path / "retry.json"
    with pytest.raises(FileExistsError):
        runner.run(1, second, "frozen")
    assert not second.exists()
