import hashlib
import json
from pathlib import Path

from scripts.evaluate_v13 import (
    PROTOCOL,
    STUDY,
    TARGET_COMMIT,
    git,
    paired_recall,
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
    assert selected["threshold"] == 0.8
    assert selected["documentation_refused"]["count"] == 1
    assert selected["out_of_scope_passed"]["count"] == 0
    assert curve[0]["out_of_scope_passed"]["count"] == 1
    assert curve[-1]["documentation_refused"]["count"] == 20


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
