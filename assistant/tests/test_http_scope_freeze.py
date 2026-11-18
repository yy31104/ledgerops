import hashlib
import json

from scripts.evaluate_http_scope import (
    MODEL,
    ROOT,
    STUDY,
    protocol,
    rows_at,
    tracked_inputs,
    valid_candidate,
)
from scripts.evaluate_v13 import git


def test_http_trial_does_not_credit_unverified_documentation_admissions():
    supported = {
        "status": "supported",
        "passage_ids": ["route.md#lookup/0"],
        "usage": {"model": MODEL, "billing": "chatgpt_plan"},
    }
    response = {"route": "docs", "admitted": True, "verification": supported}
    assert valid_candidate(response, MODEL)
    assert not valid_candidate({**response, "verification": None}, MODEL)
    assert not valid_candidate({**response, "route": None}, MODEL)
    invalid = {**response, "verification": {**supported, "status": "invalid"}}
    assert not valid_candidate(invalid, MODEL)
    assert valid_candidate({**invalid, "admitted": False}, MODEL)


def test_http_scope_freeze_binds_inputs_and_recorded_outcome():
    spec = protocol()
    manifest_path = STUDY / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    inputs = manifest["sha256"]
    assert manifest["corpus_digest"] == spec["corpus_digest"]
    receipt_path = STUDY / "run.json"
    if not receipt_path.exists():
        assert sorted(inputs) == tracked_inputs()
        for relative, digest in inputs.items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest
        return

    receipt = json.loads(receipt_path.read_text())
    freeze = receipt["freeze_commit"]
    assert receipt["verified_origin_head"] == freeze
    assert receipt["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest_path.read_bytes() == git("show", f"{freeze}:{manifest_path.relative_to(ROOT)}")
    for relative, digest in inputs.items():
        assert hashlib.sha256(git("show", f"{freeze}:{relative}")).hexdigest() == digest
        if (STUDY / "questions.jsonl").relative_to(ROOT).as_posix() == relative:
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest

    summary = json.loads((STUDY / "summary.json").read_text())
    assert summary["status"] == receipt["status"]
    assert summary["freeze_commit"] == freeze
    assert summary["raw_result_sha256"] == receipt["report_sha256"]
    assert summary["passed"] == receipt.get("passed", False)
    if summary["status"] == "completed":
        questions = rows_at(STUDY / "questions.jsonl")
        assert len(questions) == receipt["records"] == receipt["cohort_size"]
        targets = spec["targets"]
        actual = summary["candidate"]
        assert summary["targets"] == {
            "out_of_scope": (
                actual["out_of_scope_passed"]["rate"] <= targets["out_of_scope_pass_rate_max"]
            ),
            "documentation": (
                actual["documentation_refused"]["rate"]
                <= targets["documentation_false_refusal_rate_max"]
            ),
            "retrieval": (
                summary["retrieval"]["section_cluster_ci95"][0]
                >= targets["paired_recall_at_5_ci_lower_min"]
            ),
        }
        assert summary["passed"] == all(summary["targets"].values())
