"""Bind published v1.2 summaries to the release evidence, without scoring anything."""

import hashlib
import json
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1] / "eval"


def test_v12_run_summary_and_archive_share_the_same_digest():
    folder = EVAL / "v12-holdout"
    run = json.loads((folder / "run.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    archive = json.loads((EVAL / "v12-evidence-manifest.json").read_text())
    assert run["status"] == summary["status"] == "completed"
    assert run["freeze_commit"] == summary["freeze_commit"]
    assert (
        run["manifest_sha256"]
        == hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()
    )
    assert run["result_sha256"] == summary["raw_result"]["sha256"]
    assert archive["files"][summary["raw_result"]["path"]]["sha256"] == run["result_sha256"]


def test_v12_published_counts_match_the_summary():
    outcomes = json.loads((EVAL / "v12-holdout/summary.json").read_text())["outcomes"]
    assert outcomes["v1.2"]["out_of_scope_passed"]["count"] == 10
    assert outcomes["v1.1"]["out_of_scope_passed"]["count"] == 19
    assert outcomes["v1.2"]["out_of_scope_passed"]["n"] == 34
    assert outcomes["v1.2"]["documentation_refused"]["count"] == 0
    assert outcomes["v1.2"]["documentation_refused"]["n"] == 54


def test_token_and_scope_summaries_are_bound_to_archived_records():
    archive = json.loads((EVAL / "v12-evidence-manifest.json").read_text())
    for name in ("v12-token-attribution.json", "v12-dev/scope-summary.json"):
        raw = json.loads((EVAL / name).read_text())["raw_record"]
        assert archive["files"][raw["path"]]["sha256"] == raw["sha256"]
