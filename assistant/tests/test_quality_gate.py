from copy import deepcopy

import pytest

from ledgerops_assistant.quality_gate import check_regression


def baseline():
    return {
        "environment": {"quality_signal": True, "backend": "postgres", "embedder": "bge-pinned"},
        "questions": {"sha256": "frozen"},
        "retrieval": {
            name: {"by_split": {"dev": {"recall@5": 0.75}}}
            for name in ("postgres_fts", "pgvector_hnsw", "hybrid")
        },
    }


def test_gate_fails_on_recall_drop_and_accepts_equal():
    old = baseline()
    check_regression(deepcopy(old), old)
    new = deepcopy(old)
    new["retrieval"]["hybrid"]["by_split"]["dev"]["recall@5"] = 0.7499
    with pytest.raises(ValueError, match="regression"):
        check_regression(new, old)


def test_gate_rejects_wiring_metrics_and_relabelled_questions():
    old = baseline()
    for path, value in [
        (("environment", "quality_signal"), False),
        (("questions", "sha256"), "changed"),
    ]:
        new = deepcopy(old)
        new[path[0]][path[1]] = value
        with pytest.raises(ValueError):
            check_regression(new, old)


@pytest.mark.parametrize("metric", ["mrr@10", "ndcg@5", "context_recall@5"])
def test_gate_checks_ranking_and_runtime_coverage_when_baselined(metric):
    old = baseline()
    old["retrieval"]["hybrid"]["by_split"]["dev"][metric] = 0.7
    new = deepcopy(old)
    new["retrieval"]["hybrid"]["by_split"]["dev"][metric] = 0.69
    with pytest.raises(ValueError, match="regression"):
        check_regression(new, old)


def test_gate_rejects_fusion_or_corpus_drift():
    old = baseline()
    old.update(fusion={"rrf_k": 10}, corpus={"digest": "frozen"})
    for key in ("fusion", "corpus"):
        new = deepcopy(old)
        new[key] = {}
        with pytest.raises(ValueError, match="changed"):
            check_regression(new, old)
