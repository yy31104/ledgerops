"""Execute the frozen v3 protocol once; CI only checks its static hash binding.

The exclusive run.json claim is made before dependency/model/database work. Any
failure after that claim consumes the attempt. There is no retry/overwrite flag.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ASSISTANT = Path(__file__).resolve().parents[1]
ROOT = ASSISTANT.parent
STUDY = ASSISTANT / "eval/v3-holdout"
RESULT = ASSISTANT / "eval/results/v3-holdout.json"
METRICS = (
    "recall@5",
    "mrr@10",
    "ndcg@5",
    "hit@1",
    "hit@5",
    "raw_five_chunk_coverage",
    "expanded_context_coverage",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def verify_freeze(study=STUDY, root=ROOT):
    """Only byte/JSON checks: no retriever, model or database imports."""
    manifest = json.loads((study / "manifest.json").read_text())
    if manifest["sha256"] != sha(study / "questions.jsonl"):
        raise ValueError("v3 questions no longer match the frozen manifest")
    if manifest["protocol_sha256"] != sha(study / "protocol.json"):
        raise ValueError("v3 protocol changed after freeze")
    if manifest["prepared_sample_sha256"] != sha(study / "sample.json"):
        raise ValueError("v3 prepared sample changed after freeze")
    for relative, digest in manifest["code_sha256"].items():
        if sha(root / relative) != digest:
            raise ValueError(f"frozen code changed: {relative}")
    for relative, digest in manifest["corpus_file_sha256"].items():
        if sha(root / relative) != digest:
            raise ValueError(f"frozen corpus changed: {relative}")
    questions = [json.loads(line) for line in (study / "questions.jsonl").read_text().splitlines()]
    if Counter(q["category"] for q in questions) != manifest["retained_by_category"]:
        raise ValueError("v3 population changed after freeze")
    return manifest, questions


def bootstrap_summary(rows, method_names, *, resamples=10000, seed=20261106):
    """Question-weighted section cluster resampling, preserving method pairs."""
    import numpy as np

    ids = sorted(rows)
    groups = sorted({rows[key]["group"] for key in ids})
    members = [[i for i, key in enumerate(ids) if rows[key]["group"] == g] for g in groups]
    sizes = np.array([len(indices) for indices in members])
    question_draws = np.random.Generator(np.random.PCG64(seed)).integers(
        0, len(ids), size=(resamples, len(ids))
    )
    cluster_draws = np.random.Generator(np.random.PCG64(seed)).integers(
        0, len(groups), size=(resamples, len(groups))
    )
    denominators = sizes[cluster_draws].sum(axis=1)

    def interval(samples):
        return [float(x) for x in np.percentile(samples, [2.5, 97.5], method="linear")]

    def distributions(values):
        sums = np.array([values[indices].sum() for indices in members])
        return values[question_draws].mean(axis=1), sums[cluster_draws].sum(axis=1) / denominators

    estimates, values_by_method = {}, {}
    for method in method_names:
        estimates[method], values_by_method[method] = {}, {}
        for metric in METRICS:
            values = np.array([rows[key][method][metric] for key in ids], dtype=float)
            values_by_method[method][metric] = values
            question, cluster = distributions(values)
            estimates[method][metric] = {
                "mean": float(values.mean()),
                "question_ci95": interval(question),
                "section_cluster_ci95": interval(cluster),
            }
    comparisons = {}
    for comparator in ("legacy_hybrid", "pgvector_hnsw", "postgres_fts"):
        comparisons[f"hybrid - {comparator}"] = {}
        for metric in ("recall@5", "mrr@10", "hit@1"):
            differences = values_by_method["hybrid"][metric] - values_by_method[comparator][metric]
            question, cluster = distributions(differences)
            intervals = {
                "paired_question_ci95": interval(question),
                "paired_section_cluster_ci95": interval(cluster),
            }
            interpretation = {
                key: "no detectable difference"
                if low <= 0 <= high
                else "hybrid higher"
                if low > 0
                else "hybrid lower"
                for key, (low, high) in intervals.items()
            }
            comparisons[f"hybrid - {comparator}"][metric] = {
                "mean_difference": float(differences.mean()),
                **intervals,
                "interpretation": interpretation,
            }
    return {
        "n": len(ids),
        "groups": dict(zip(groups, sizes.tolist(), strict=True)),
        "resamples": resamples,
        "seed": seed,
        "methods": estimates,
        "comparisons": comparisons,
    }


class CachedRetriever:
    """Reuse identical query/k calls, preserving each original retriever's output."""

    def __init__(self, delegate, name, counters=None):
        self.delegate, self.name, self.counters = delegate, name, counters
        self.cache = {}

    def search(self, query, k=10):
        key = (query, k)
        if key not in self.cache:
            self.cache[key] = self.delegate.search(query, k)
            if self.counters is not None:
                self.counters[f"{self.name}:k={k}"] += 1
        return self.cache[key]


def execute(record, freeze_commit):
    # All application/dependency imports follow the durable attempt claim.
    from evaluate_local import temporary_database
    from mlflow import MlflowClient

    from ledgerops_assistant.answer import ScopeGate, answer
    from ledgerops_assistant.context import ContextRetriever
    from ledgerops_assistant.corpus import load_corpus
    from ledgerops_assistant.embeddings import BgeSmallEmbedder
    from ledgerops_assistant.evaluation import score_ranking
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
    from ledgerops_assistant.providers.fake import FakeProvider
    from ledgerops_assistant.retrieval import HybridRetriever, rank_sections

    class ObservedFake:
        name = "fake"

        def __init__(self):
            self.given = []

        def draft(self, question, passages):
            self.given = list(passages)
            return FakeProvider().draft(question, passages)

    def coverage(chunks, relevant):
        return len({c.section_id for c in chunks} & relevant) / len(relevant)

    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        != freeze_commit
    ):
        raise ValueError("execution must use the exact pushed freeze commit")
    manifest, questions = verify_freeze()
    protocol = json.loads((STUDY / "protocol.json").read_text())
    corpus = load_corpus(ROOT)
    if corpus.digest != manifest["corpus_digest"]:
        raise ValueError("corpus digest differs from freeze")
    report = {
        "status": "started",
        "attempt": 1,
        "freeze_commit": freeze_commit,
        "manifest_sha256": sha(STUDY / "manifest.json"),
        "protocol": {
            "commit": manifest["protocol_commit"],
            "sha256": manifest["protocol_sha256"],
            "pre_freeze_population_amendments": manifest["protocol_deviations"],
        },
        "corpus": {
            "revision": corpus.revision,
            "source_commit": manifest["source_commit"],
            "digest": corpus.digest,
            "files": len(corpus.file_digests),
            "chunks": len(corpus.chunks),
            "sections": len(corpus.section_ids()),
        },
        "questions": {
            "sha256": manifest["sha256"],
            "total": len(questions),
            "retrieval_scored": sum(q["expected_route"] == "docs" for q in questions),
            "by_category": manifest["retained_by_category"],
            "excluded_before_freeze": manifest["exclusions"],
        },
        "configuration": protocol["evaluation"],
        "environment": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "packages": {
                p: metadata.version(p)
                for p in ["numpy", "onnxruntime", "tokenizers", "psycopg", "mlflow-skinny"]
            },
        },
        "per_question": {},
        "answers": {},
    }
    record["stage"] = "tracking_setup"
    cache = ASSISTANT / ".cache/v3-holdout"
    cache.mkdir(parents=True, exist_ok=True)
    client = MlflowClient(tracking_uri=f"sqlite:///{cache.resolve() / 'tracking.db'}")
    experiment = client.get_experiment_by_name("ledgerops-v3-holdout")
    experiment_id = (
        experiment.experiment_id
        if experiment
        else client.create_experiment(
            "ledgerops-v3-holdout", artifact_location=(cache.resolve() / "artifacts").as_uri()
        )
    )
    run_id = client.create_run(experiment_id).info.run_id
    record["mlflow_run_id"] = report["mlflow_run_id"] = run_id
    client.log_param(run_id, "freeze_commit", freeze_commit)
    client.log_param(run_id, "questions_sha256", manifest["sha256"])
    client.log_param(run_id, "protocol_sha256", manifest["protocol_sha256"])
    client.log_param(run_id, "manifest_sha256", report["manifest_sha256"])
    client.log_param(run_id, "corpus_digest", corpus.digest)
    client.log_param(run_id, "configuration", json.dumps(protocol["evaluation"], sort_keys=True))
    try:
        record["stage"] = "embedding_and_index_setup"
        write_json(STUDY / "run.json", record)
        embedder = BgeSmallEmbedder()
        report["environment"]["embedder"] = embedder.name
        sql_counts = Counter()
        with temporary_database() as dsn:
            setup(dsn)
            corpus_id = index_corpus(dsn, corpus, embedder)
            fts = CachedRetriever(
                PostgresRetriever(dsn, corpus_id, embedder, method="fts"),
                "postgres_fts",
                sql_counts,
            )
            vector = CachedRetriever(
                PostgresRetriever(dsn, corpus_id, embedder, method="vector"),
                "pgvector_hnsw",
                sql_counts,
            )
            methods = {
                "hybrid": CachedRetriever(HybridRetriever(fts, vector, rrf_k=10), "hybrid"),
                "legacy_hybrid": CachedRetriever(
                    HybridRetriever(fts, vector, rrf_k=60), "legacy_hybrid"
                ),
                "pgvector_hnsw": vector,
                "postgres_fts": fts,
            }
            contexts = {name: ContextRetriever(r, corpus) for name, r in methods.items()}
            gate = ScopeGate(vector, protocol["evaluation"]["scope_gate"]["threshold"])
            record["stage"] = "scoring_frozen_questions"
            for q in questions:
                row = {"group": q["group"]}
                if q["expected_route"] == "docs":
                    relevant = set(q["relevant_sections"])
                    for name, retriever in methods.items():
                        hits = retriever.search(q["question"], 30)
                        provider = ObservedFake()
                        result = answer(
                            q["question"],
                            contexts[name],
                            provider,
                            gate=gate,
                            revision=corpus.revision,
                        )
                        selected = [corpus.by_id()[key] for key in result.retrieved]
                        row[name] = {
                            **score_ranking(rank_sections(hits), q["relevant_sections"]),
                            "ranked_sections": rank_sections(hits),
                            "raw_first_five_chunks": [h.chunk.chunk_id for h in hits[:5]],
                            "raw_five_chunk_coverage": coverage(
                                [h.chunk for h in hits[:5]], relevant
                            ),
                            "expanded_context_coverage": coverage(provider.given, relevant),
                            "selected_context_coverage": coverage(selected, relevant),
                            "selected_context_chunks": result.retrieved,
                            "provided_context_chunks": [c.chunk_id for c in provider.given],
                            "provided_json_bytes": len(
                                json.dumps([asdict(c) for c in provider.given]).encode()
                            ),
                            "refused": result.refused,
                            "reason": result.reason,
                        }
                        if name == "hybrid":
                            report["answers"][q["id"]] = result.to_dict()
                    report["per_question"][q["id"]] = row
                else:
                    result = answer(
                        q["question"],
                        contexts["hybrid"],
                        ObservedFake(),
                        gate=gate,
                        revision=corpus.revision,
                    )
                    report["answers"][q["id"]] = result.to_dict()
                record["completed_question_ids"].append(q["id"])
                record["sql_search_counts"] = dict(sql_counts)
                write_json(cache / "partial.json", report)
                write_json(STUDY / "run.json", record)
        record["stage"] = "prespecified_bootstrap"
        write_json(STUDY / "run.json", record)
        report["statistics"] = bootstrap_summary(report["per_question"], list(methods))
        non_docs = [q for q in questions if q["expected_route"] != "docs"]
        confusion = Counter(
            (q["expected_route"], report["answers"][q["id"]]["route"]) for q in non_docs
        )
        report["non_documentation"] = {
            "n": len(non_docs),
            "provider": "fake",
            "route_accuracy": sum(
                report["answers"][q["id"]]["route"] == q["expected_route"] for q in non_docs
            )
            / len(non_docs),
            "by_category": {
                category: {
                    "n": len(subset := [q for q in non_docs if q["category"] == category]),
                    "correct_route": sum(
                        report["answers"][q["id"]]["route"] == q["expected_route"] for q in subset
                    ),
                    "refused": sum(report["answers"][q["id"]]["refused"] for q in subset),
                }
                for category in sorted({q["category"] for q in non_docs})
            },
            "confusion": {
                f"{expected} -> {actual}": n for (expected, actual), n in confusion.items()
            },
            "correct_refusals": sum(report["answers"][q["id"]]["refused"] for q in non_docs),
            "missed_refusals": sum(not report["answers"][q["id"]]["refused"] for q in non_docs),
        }
        report["status"] = "completed"
        report["completed_at"] = now()
        report["context_measurement"] = (
            "Expanded coverage counts only passages actually supplied to FakeProvider. "
            "Pre-generation routing/scope refusals have zero provided coverage; selected "
            "candidate coverage is also preserved separately."
        )
        write_json(RESULT, report)
        for method, metrics in report["statistics"]["methods"].items():
            for metric, values in metrics.items():
                client.log_metric(run_id, f"{method}.{metric.replace('@', '_at_')}", values["mean"])
        client.log_dict(run_id, report, "v3-holdout.json")
        client.set_terminated(run_id)
    except Exception:
        report["status"] = "failed"
        report["failed_stage"] = record["stage"]
        write_json(RESULT, report)
        client.log_dict(run_id, report, "partial-v3-holdout.json")
        client.set_terminated(run_id, status="FAILED")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--freeze-commit", required=True)
    args = parser.parse_args()
    record = {
        "attempt": 1,
        "status": "started",
        "started_at": now(),
        "freeze_commit": args.freeze_commit,
        "stage": "initializing",
        "completed_question_ids": [],
    }
    try:
        with (STUDY / "run.json").open("x") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        print("The v3 attempt is already claimed; execution refused.", file=sys.stderr)
        return 2
    started = time.monotonic()
    try:
        execute(record, args.freeze_commit)
        record.update(status="completed", result_sha256=sha(RESULT))
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__)
        failure = ASSISTANT / ".cache/v3-holdout/failure.txt"
        failure.parent.mkdir(parents=True, exist_ok=True)
        failure.write_text(traceback.format_exc())
        failure.chmod(0o600)
        if not RESULT.exists():
            write_json(
                RESULT,
                {
                    "status": "failed",
                    "attempt": 1,
                    "freeze_commit": args.freeze_commit,
                    "failed_stage": record["stage"],
                    "error_type": type(error).__name__,
                },
            )
        record["result_sha256"] = sha(RESULT)
        print(f"V3 attempt failed at {record['stage']}: {type(error).__name__}", file=sys.stderr)
    record.update(finished_at=now(), elapsed_seconds=time.monotonic() - started)
    write_json(STUDY / "run.json", record)
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
