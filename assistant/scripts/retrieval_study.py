"""Dev-only fusion selection and a single frozen blind retrieval comparison."""

import argparse
import hashlib
import itertools
import json
import platform
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from evaluate_local import temporary_database

from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder
from ledgerops_assistant.evaluation import evaluate_retriever, load_questions, paired_bootstrap
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
from ledgerops_assistant.retrieval import HybridRetriever
from ledgerops_assistant.tracking import log_experiment

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / ".cache/study-v2"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(corpus, embedder, path, questions, split):
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "corpus": {"digest": corpus.digest, "revision": corpus.revision},
        "questions": {
            "sha256": sha(path),
            "evaluated_split": split,
            "total": len(questions),
            "groups": len({q["group"] for q in questions}),
        },
        "environment": {
            "embedder": embedder.name,
            "backend": "postgres",
            "quality_signal": True,
            "python": platform.python_version(),
        },
    }


def methods(dsn, corpus, embedder):
    setup(dsn)
    key = index_corpus(dsn, corpus, embedder)
    return (
        PostgresRetriever(dsn, key, embedder, method="fts"),
        PostgresRetriever(dsn, key, embedder, method="vector"),
    )


class Cached:
    def __init__(self, retriever, questions):
        self.name = retriever.name
        self.hits = {q["question"]: retriever.search(q["question"], 50) for q in questions}

    def search(self, query, k):
        return self.hits[query][:k]


def record(report):
    report["mlflow_run_id"] = log_experiment(report, ROOT / ".cache/mlflow")
    return report


def compare(base, other, questions):
    """Question and topic-cluster paired bootstrap; topics are the resampling unit."""
    result = {}
    groups = sorted({q["group"] for q in questions})
    for metric in ("recall@5", "mrr@10", "ndcg@5", "context_recall@5"):
        pair = paired_bootstrap(base, other, metric, resamples=10000)
        clustered = [
            [
                other[q["id"]][metric] - base[q["id"]][metric]
                for q in questions
                if q["group"] == group
            ]
            for group in groups
        ]
        rng = np.random.default_rng(0)
        means = []
        for sample in rng.integers(0, len(groups), size=(10000, len(groups))):
            values = [value for i in sample for value in clustered[i]]
            means.append(sum(values) / len(values))
        pair["cluster_ci95"] = [round(float(x), 4) for x in np.percentile(means, [2.5, 97.5])]
        pair["clusters"] = len(groups)
        result[metric] = pair
    return result


def tune(dsn):
    protocol = json.loads((STUDY / "protocol.json").read_text())
    path = ROOT / "eval/v1-draft/questions.jsonl"
    corpus, embedder = load_corpus(), BgeSmallEmbedder()
    questions = [
        q
        for q in load_questions(path, corpus.section_ids())
        if q["split"] == "dev" and q["expected_route"] == "docs"
    ]
    keyword, vector = [Cached(r, questions) for r in methods(dsn, corpus, embedder)]
    baseline = evaluate_retriever(HybridRetriever(keyword, vector, rrf_k=60), questions)
    raw = {
        r.name: {query: [asdict(h) for h in hits] for query, hits in r.hits.items()}
        for r in (keyword, vector)
    }
    (STUDY / "dev-rankings.json").write_text(json.dumps(raw, indent=2) + "\n")
    grid = protocol["grid"]
    trials = []
    for weight, rrf_k, depth, unit in itertools.product(
        grid["keyword_weight"], grid["rrf_k"], grid["depth"], grid["unit"]
    ):
        config = {"weights": [weight, 1.0], "rrf_k": rrf_k, "depth": depth, "unit": unit}
        scores = evaluate_retriever(HybridRetriever(keyword, vector, **config), questions)
        report = metadata(corpus, embedder, path, questions, "dev")
        report.update(retrieval={"hybrid": scores}, fusion=config)
        report["environment"]["latency_kind"] = "cached-fusion-only"
        record(report)
        trials.append(report)
    old = baseline["all"]
    eligible = [
        r
        for r in trials
        if all(r["retrieval"]["hybrid"]["all"][m] >= old[m] for m in ("mrr@10", "ndcg@5"))
    ]
    selected = max(
        eligible,
        key=lambda r: (
            *(r["retrieval"]["hybrid"]["all"][m] for m in ("recall@5", "ndcg@5", "mrr@10")),
            json.dumps(r["fusion"], sort_keys=True),
        ),
    )
    selection = {
        "protocol": protocol,
        "legacy_dev": baseline["all"],
        "selected_config": selected["fusion"],
        "selected_dev": selected["retrieval"]["hybrid"]["all"],
        "selected_mlflow_run_id": selected["mlflow_run_id"],
        "corpus_digest": corpus.digest,
        "questions_sha256": sha(path),
        "embedder": embedder.name,
        "trials": [
            {
                "fusion": r["fusion"],
                "metrics": r["retrieval"]["hybrid"]["all"],
                "mlflow_run_id": r["mlflow_run_id"],
            }
            for r in trials
        ],
    }
    (STUDY / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    print(json.dumps({k: v for k, v in selection.items() if k != "trials"}, indent=2))


def validate(dsn, question_path, freeze_path, output):
    if output.exists():
        raise ValueError("refusing to overwrite an existing validation run")
    freeze = json.loads(freeze_path.read_text())
    corpus, embedder = load_corpus(), BgeSmallEmbedder()
    if freeze["corpus_digest"] != corpus.digest or freeze["embedder"] != embedder.name:
        raise ValueError("frozen corpus/model changed")
    if freeze["holdout_sha256"] != sha(question_path):
        raise ValueError("frozen holdout changed")
    for relative, expected in freeze["code_sha256"].items():
        if sha(ROOT / relative) != expected:
            raise ValueError(f"frozen study code changed: {relative}")
    attempt_path = freeze_path.with_name("validation-attempt.json")
    attempt = {
        "status": "started",
        "created_at": datetime.now(UTC).isoformat(),
        "freeze_sha256": sha(freeze_path),
        "output": output.name,
    }
    # One canonical reservation per freeze, including interrupted/failed attempts.
    # A different output name cannot silently reuse the held-out questions.
    with attempt_path.open("x") as stream:
        json.dump(attempt, stream, indent=2)
    questions = load_questions(question_path, corpus.section_ids())
    if any(q["split"] != "test" or q["expected_route"] != "docs" for q in questions):
        raise ValueError("study validation requires a retrieval-only holdout")
    keyword, vector = methods(dsn, corpus, embedder)
    retrievers = {
        "postgres_fts": keyword,
        "pgvector_hnsw": vector,
        "pgvector_exact": PostgresRetriever(dsn, vector.corpus_id, embedder, method="exact"),
        "legacy_hybrid": HybridRetriever(keyword, vector, rrf_k=60),
        "hybrid": HybridRetriever(keyword, vector, **freeze["selected_config"]),
    }
    report = metadata(corpus, embedder, question_path, questions, "test")
    report["freeze_sha256"] = sha(freeze_path)
    report["fusion"] = freeze["selected_config"]
    report["retrieval"] = {name: evaluate_retriever(r, questions) for name, r in retrievers.items()}
    report["comparisons"] = {
        f"{name} -> hybrid": compare(
            report["retrieval"][name]["per_question"],
            report["retrieval"]["hybrid"]["per_question"],
            questions,
        )
        for name in ("legacy_hybrid", "pgvector_hnsw")
    }
    record(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    attempt.update(
        status="completed", result_sha256=sha(output), mlflow_run_id=report["mlflow_run_id"]
    )
    attempt_path.write_text(json.dumps(attempt, indent=2) + "\n")
    print(json.dumps({k: v["all"] for k, v in report["retrieval"].items()}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("tune", "validate"))
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with temporary_database() as dsn:
        if args.mode == "tune":
            tune(dsn)
        else:
            validate(dsn, args.questions, args.freeze, args.output)
