import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts import evaluate_v11 as study


@pytest.mark.parametrize(
    "difference,interval,method",
    [
        (0.1, [0.01, 0.2], "hybrid"),
        (0.1, [-0.02, 0.2], "vector"),
        (0.1, [0, 0.2], "vector"),
        (-0.1, [-0.2, -0.01], "vector"),
    ],
)
def test_selection_rule_is_fixed_before_results(difference, interval, method):
    assert (
        study.choose_default(
            {"mean_difference": difference, "paired_section_cluster_ci95": interval}
        )
        == method
    )


def test_cache_keeps_component_metadata_and_distinct_query_limits():
    calls = []
    delegate = SimpleNamespace(
        name="hybrid",
        retrievers=("vector-component",),
        search=lambda query, k: calls.append((query, k)) or [],
    )
    cached = study.Cached(delegate)
    assert cached.retrievers == ("vector-component",)
    cached.search("q", 30)
    cached.search("q", 30)
    cached.search("q", 50)
    assert calls == [("q", 30), ("q", 50)]


def test_quote_coverage_requires_the_quoted_section():
    question = {
        "key_facts": ["one", "two"],
        "evidence": [
            {"fact_index": 0, "section_id": "a", "quote": "Exact source words"},
            {"fact_index": 1, "section_id": "b", "quote": "Other source words"},
        ],
    }
    chunks = [
        SimpleNamespace(section_id="a", text="Exact\n source words"),
        SimpleNamespace(section_id="unrelated", text="Other source words"),
    ]
    assert study.quote_coverage(chunks, question) == 0.5


def test_existing_result_cannot_be_replaced_or_consume_an_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "verify", lambda *args: {"split": "dev"})
    output = tmp_path / "result.json"
    output.write_text("original")
    with pytest.raises(FileExistsError):
        study.run(output, tmp_path)
    assert output.read_text() == "original" and not (tmp_path / "run.json").exists()


def test_changed_frozen_inputs_fail_before_attempt_or_runtime(tmp_path):
    path = tmp_path / "question.jsonl"
    path.write_text("original")
    manifest = {
        "inputs_sha256": {"question.jsonl": hashlib.sha256(path.read_bytes()).hexdigest()},
        "code_sha256": {},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert study.verify(tmp_path, tmp_path) == manifest
    path.write_text("changed")
    with pytest.raises(ValueError, match="frozen input"):
        study.verify(tmp_path, tmp_path)
