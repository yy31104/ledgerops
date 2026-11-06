"""Static v3 integrity and synthetic statistics only; never evaluate v3 in CI."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_v3.py"
SPEC = importlib.util.spec_from_file_location("v3_protocol", SCRIPT)
v3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v3)


def test_v3_questions_and_manifest_are_bound_without_retrieval():
    manifest, questions = v3.verify_freeze()
    assert manifest["status"] == "frozen_synthetic"
    assert len(questions) == 69
    assert len({q["id"] for q in questions}) == 69
    assert {e["id"] for e in manifest["exclusions"]} == {"v3-039", "v3-052", "v3-066"}
    assert all(q["split"] == "test" for q in questions)
    assert not (v3.STUDY / "AUTHOR-NOTES.md").exists()


def test_v3_changed_question_bytes_fail_before_any_runtime_import(tmp_path):
    (tmp_path / "manifest.json").write_bytes((v3.STUDY / "manifest.json").read_bytes())
    (tmp_path / "questions.jsonl").write_bytes((v3.STUDY / "questions.jsonl").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="questions no longer match"):
        v3.verify_freeze(tmp_path)


def test_section_cluster_bootstrap_weights_questions_not_cluster_means():
    names = ("hybrid", "legacy_hybrid", "pgvector_hnsw", "postgres_fts")
    rows = {
        key: {"group": group, **{name: dict.fromkeys(v3.METRICS, value) for name in names}}
        for key, group, value in [("a", "large", 0), ("b", "large", 0), ("c", "small", 1)]
    }
    # PCG64(1)'s one two-cluster draw is [large, small]. Its three questions
    # average 1/3; averaging the two cluster means would incorrectly give 1/2.
    result = v3.bootstrap_summary(rows, names, resamples=1, seed=1)
    metric = result["methods"]["hybrid"]["recall@5"]
    assert metric["mean"] == pytest.approx(1 / 3)
    assert metric["section_cluster_ci95"] == pytest.approx([1 / 3, 1 / 3])
    pair = result["comparisons"]["hybrid - legacy_hybrid"]["recall@5"]
    assert set(pair["interpretation"].values()) == {"no detectable difference"}
    assert json.dumps(result)
