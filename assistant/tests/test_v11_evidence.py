"""Bind published aggregates to immutable release evidence, without scoring a holdout."""

import hashlib
import json
from pathlib import Path

from scripts.evaluate_v11 import choose_default

EVAL = Path(__file__).resolve().parents[1] / "eval"


def test_holdout_result_archive_and_run_record_share_the_same_digest():
    folder = EVAL / "v11-holdout"
    run = json.loads((folder / "run.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    archive = json.loads((EVAL / "v11-evidence-manifest.json").read_text())
    assert run["status"] == summary["status"] == "completed"
    assert run["freeze_commit"] == summary["freeze_commit"]
    assert (
        run["manifest_sha256"]
        == hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()
    )
    assert run["result_sha256"] == summary["raw_result"]["sha256"]
    assert archive["files"][summary["raw_result"]["path"]]["sha256"] == run["result_sha256"]
    assert summary["decision"]["method"] == choose_default(summary["decision"]["comparison"])


def test_ungraded_answer_summary_is_bound_to_the_release_record():
    summary = json.loads((EVAL / "v11-answer-summary.json").read_text())
    archive = json.loads((EVAL / "v11-evidence-manifest.json").read_text())
    assert (
        archive["files"]["assistant/eval/results/v1.1-human-review.json"]["sha256"]
        == summary["record_sha256"]
    )
    assert summary["human_judgements"]["completed"] == 0
    assert summary["human_judgements"]["pending"] == summary["generation"]["recorded"] == 32
