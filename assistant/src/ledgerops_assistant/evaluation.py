"""Measure retrieval, routing and grounded answering on the labelled question set.

    uv run --locked python -m ledgerops_assistant.evaluation --output eval/results/run.json

Labels name sections (``path#anchor``), not chunks, so changing the chunker does not
invalidate them. Retrieval is scored on the ranked list of distinct sections.

- Hit@k: share of questions with at least one relevant section in the top k.
- Recall@5: share of a question's relevant sections found in the top 5, averaged.
- MRR@10: 1 / rank of the first relevant section (0 if not in the top 10), averaged.

Only questions the docs should answer are scored for retrieval. Routing and the scope
gate are scored on every question. The gate threshold is chosen on the dev split only;
the test split shows whether that choice holds on questions it was not chosen on.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from ledgerops_assistant import routing
from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.context import CONTEXT_POLICY, MAX_CONTEXT_BYTES, ContextRetriever
from ledgerops_assistant.corpus import CHUNKER_VERSION, load_corpus
from ledgerops_assistant.providers import get_provider
from ledgerops_assistant.retrieval import (
    BM25Retriever,
    HybridRetriever,
    VectorRetriever,
    rank_sections,
)

ASSISTANT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONS = ASSISTANT_ROOT / "eval" / "v1-draft" / "questions.jsonl"
CACHE_DIR = ASSISTANT_ROOT / ".cache" / "index"


def load_questions(path: Path, sections: set[str]) -> list[dict]:
    questions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    ids = Counter(q["id"] for q in questions)
    if duplicates := [i for i, n in ids.items() if n > 1]:
        raise ValueError(f"duplicate question IDs: {duplicates}")
    for q in questions:
        if unknown := [s for s in q["relevant_sections"] if s not in sections]:
            raise ValueError(f"{q['id']} names sections missing from the corpus: {unknown}")
        if q["expected_route"] == routing.DOCS and not q["relevant_sections"]:
            raise ValueError(f"{q['id']} is a docs question without relevant sections")
    groups = defaultdict(set)
    for q in questions:
        groups[q["group"]].add(q["split"])
    if leaks := [g for g, splits in groups.items() if len(splits) > 1]:
        raise ValueError(f"groups split across dev and test: {leaks}")
    return questions


def _mean(values) -> float:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else 0.0


def score_ranking(ranked: list[str], relevant: list[str]) -> dict:
    ranked = list(dict.fromkeys(ranked))
    relevant = set(relevant)
    first = next((i for i, s in enumerate(ranked[:10], start=1) if s in relevant), None)
    return {
        "hit@1": float(bool(set(ranked[:1]) & relevant)),
        "hit@3": float(bool(set(ranked[:3]) & relevant)),
        "hit@5": float(bool(set(ranked[:5]) & relevant)),
        "recall@5": len(set(ranked[:5]) & relevant) / len(relevant) if relevant else 0.0,
        "mrr@10": 1 / first if first else 0.0,
        "ndcg@5": (
            sum(1 / math.log2(i + 2) for i, section in enumerate(ranked[:5]) if section in relevant)
            / sum(1 / math.log2(i + 2) for i in range(min(5, len(relevant))))
            if relevant
            else 0.0
        ),
    }


def evaluate_retriever(retriever, questions: list[dict]) -> dict:
    rows, latencies = [], []
    for q in questions:
        started = time.perf_counter()
        hits = retriever.search(q["question"], 30)
        latencies.append((time.perf_counter() - started) * 1000)
        ranked = rank_sections(hits)
        rows.append(
            {
                "q": q,
                "ranked": ranked,
                **score_ranking(ranked, q["relevant_sections"]),
                # Preserve the historical raw-five-chunk coverage for comparison.
                # Expanded generation context is measured separately below.
                "context_recall@5": score_ranking(rank_sections(hits[:5]), q["relevant_sections"])[
                    "recall@5"
                ],
            }
        )

    def summarize(subset):
        keys = ("hit@1", "hit@3", "hit@5", "recall@5", "mrr@10", "ndcg@5", "context_recall@5")
        return {"n": len(subset), **{k: _mean(r[k] for r in subset) for k in keys}}

    by_split = {s: summarize([r for r in rows if r["q"]["split"] == s]) for s in ("dev", "test")}
    categories = sorted({r["q"]["category"] for r in rows})
    return {
        "all": summarize(rows),
        "by_split": by_split,
        "by_category": {
            c: summarize([r for r in rows if r["q"]["category"] == c]) for c in categories
        },
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 3)
            if latencies
            else 0.0,
        },
        "misses": [
            {
                "id": r["q"]["id"],
                "question": r["q"]["question"],
                "relevant": r["q"]["relevant_sections"],
                "top3": r["ranked"][:3],
            }
            for r in rows
            if not r["hit@5"]
        ],
        "per_question": {
            r["q"]["id"]: {
                key: r[key]
                for key in ("hit@1", "hit@5", "recall@5", "mrr@10", "ndcg@5", "context_recall@5")
            }
            for r in rows
        },
    }


def paired_bootstrap(base: dict, other: dict, metric: str, *, resamples=2000, seed=0) -> dict:
    """Is ``other`` better than ``base`` on the same questions, beyond resampling noise?

    Resample the questions with replacement 2,000 times and recompute the mean
    difference each time; the middle 95% of those means is the interval. An interval
    that contains 0 means this question set cannot tell the two methods apart.
    """
    import numpy as np

    ids = sorted(base)
    diffs = np.array([other[i][metric] - base[i][metric] for i in ids])
    rng = np.random.default_rng(seed)
    means = diffs[rng.integers(0, len(diffs), size=(resamples, len(diffs)))].mean(axis=1)
    return {
        "mean_difference": round(float(diffs.mean()), 4),
        "ci95": [
            round(float(np.percentile(means, 2.5)), 4),
            round(float(np.percentile(means, 97.5)), 4),
        ],
        "better_on": [i for i, d in zip(ids, diffs, strict=True) if d > 0],
        "worse_on": [i for i, d in zip(ids, diffs, strict=True) if d < 0],
    }


def calibrate_gate(gate_retriever, questions: list[dict]) -> dict:
    """Pick the cosine threshold that best separates docs from out-of-scope on dev."""
    scored = []
    for q in questions:
        if q["expected_route"] not in (routing.DOCS, routing.OUT_OF_SCOPE):
            continue
        if routing.classify(q["question"]) != routing.DOCS:
            continue
        hits = gate_retriever.search(q["question"], 1)
        scored.append((q, hits[0].score if hits else 0.0))
    dev = [(q, s) for q, s in scored if q["split"] == "dev"]

    def accuracy(rows, threshold):
        right = sum((s >= threshold) == (q["expected_route"] == routing.DOCS) for q, s in rows)
        return right / len(rows) if rows else 0.0

    test_rows = [r for r in scored if r[0]["split"] == "test"]
    candidates = sorted({round(s, 4) for _, s in dev})
    best = max(candidates, key=lambda t: (accuracy(dev, t), -t))
    return {
        "threshold": best,
        "dev_accuracy": round(accuracy(dev, best), 4),
        "test_accuracy": round(accuracy(test_rows, best), 4) if test_rows else None,
        "scores": {
            "docs": sorted(round(s, 4) for q, s in scored if q["expected_route"] == routing.DOCS),
            "out_of_scope": {
                q["id"]: round(s, 4)
                for q, s in scored
                if q["expected_route"] == routing.OUT_OF_SCOPE
            },
        },
    }


def evaluate_answers(questions, retriever, provider, gate, revision) -> dict:
    rows = []
    for q in questions:
        result = answer(q["question"], retriever, provider, gate=gate, revision=revision)
        predicted_route = routing.OUT_OF_SCOPE if result.reason == "not_in_docs" else result.route
        cited = {c.section_id for c in result.citations}
        rows.append(
            {
                "id": q["id"],
                "split": q["split"],
                "expected_route": q["expected_route"],
                "predicted_route": predicted_route,
                "refused": result.refused,
                "reason": result.reason,
                "should_refuse": q["expected_route"] != routing.DOCS,
                "cites_relevant": bool(cited & set(q["relevant_sections"])),
                "dropped_citations": len(result.dropped_citations),
                "answer": result.text,
                "citations": [c.chunk_id for c in result.citations],
                "context_chunks": result.retrieved,
            }
        )
    answered_docs = [r for r in rows if r["expected_route"] == routing.DOCS and not r["refused"]]
    confusion = Counter((r["expected_route"], r["predicted_route"]) for r in rows)
    return {
        "provider": provider.name,
        "route_accuracy": {
            split: _mean(
                r["expected_route"] == r["predicted_route"] for r in rows if r["split"] == split
            )
            for split in ("dev", "test")
            if any(r["split"] == split for r in rows)
        },
        "refusal": {
            "correct_refusals": sum(r["refused"] and r["should_refuse"] for r in rows),
            "missed_refusals": sum(not r["refused"] and r["should_refuse"] for r in rows),
            "wrong_refusals": sum(r["refused"] and not r["should_refuse"] for r in rows),
            "should_refuse": sum(r["should_refuse"] for r in rows),
            "should_answer": sum(not r["should_refuse"] for r in rows),
        },
        "answered_docs_questions": len(answered_docs),
        "citation_hit_rate": _mean(r["cites_relevant"] for r in answered_docs),
        "dropped_citations": sum(r["dropped_citations"] for r in rows),
        "confusion": {f"{e} -> {p}": n for (e, p), n in sorted(confusion.items())},
        "answers": rows,
    }


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def check_freeze_record(questions_path: Path) -> None:
    record_path = questions_path.with_name("manifest.json")
    if not record_path.exists():
        record_path = questions_path.with_name("review.json")
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    if (
        record.get("status") not in ("approved", "frozen_unreviewed", "frozen_synthetic")
        or record.get("sha256") != hashlib.sha256(questions_path.read_bytes()).hexdigest()
    ):
        raise ValueError("holdout requires a frozen record for this exact question set")


def run(args) -> dict:
    corpus = load_corpus()
    questions_path = Path(args.questions)
    all_questions = load_questions(questions_path, corpus.section_ids())
    if args.split != "dev":
        check_freeze_record(questions_path)
    questions = [q for q in all_questions if args.split == "all" or q["split"] == args.split]
    docs_questions = [q for q in questions if q["expected_route"] == routing.DOCS]

    if args.embedder == "bge":
        from ledgerops_assistant.embeddings import BgeSmallEmbedder

        embedder = BgeSmallEmbedder()
        ablation = BgeSmallEmbedder(use_instruction=False)
    else:
        from ledgerops_assistant.embeddings import HashingEmbedder

        embedder, ablation = HashingEmbedder(384 if args.backend == "postgres" else 512), None

    if args.backend == "postgres":
        from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup

        dsn = os.environ.get("LEDGEROPS_ASSISTANT_DATABASE_URL")
        if not dsn:
            raise ValueError("set LEDGEROPS_ASSISTANT_DATABASE_URL to the assistant database")
        if args.setup_database:
            setup(dsn)
        key = index_corpus(dsn, corpus, embedder)
        keyword = PostgresRetriever(dsn, key, embedder, method="fts")
        vector = PostgresRetriever(dsn, key, embedder, method="vector")
        exact = PostgresRetriever(dsn, key, embedder, method="exact")
        methods = {"postgres_fts": keyword, "pgvector_hnsw": vector, "pgvector_exact": exact}
    else:
        keyword = BM25Retriever(corpus)
        vector = VectorRetriever(corpus, embedder, cache_dir=CACHE_DIR)
        methods = {"bm25": keyword, "vector": vector}
    fusion_k = 60 if getattr(args, "fusion", "tuned") == "legacy" else 10
    hybrid = HybridRetriever(keyword, vector, rrf_k=fusion_k)
    methods["legacy_hybrid"] = HybridRetriever(keyword, vector, rrf_k=60)
    methods["hybrid"] = hybrid
    if ablation is not None and args.backend == "memory":
        methods["vector_no_instruction"] = VectorRetriever(corpus, ablation, cache_dir=CACHE_DIR)

    retrieval = {name: evaluate_retriever(r, docs_questions) for name, r in methods.items()}
    names = list(methods)
    pairs = [(names[0], "hybrid"), (names[0], names[1]), (names[1], "hybrid")]
    if "vector_no_instruction" in retrieval:
        pairs.append(("vector_no_instruction", "vector"))
    comparisons = {
        f"{b} -> {o}": {
            metric: paired_bootstrap(
                retrieval[b]["per_question"], retrieval[o]["per_question"], metric
            )
            for metric in ("hit@1", "mrr@10")
        }
        for b, o in pairs
    }
    gate_report = calibrate_gate(vector, [q for q in all_questions if q["split"] == "dev"])
    gate = ScopeGate(vector, gate_report["threshold"])
    expanded = getattr(args, "context", "expanded") == "expanded"
    answer_retriever = ContextRetriever(hybrid, corpus) if expanded else hybrid
    answers = evaluate_answers(
        questions, answer_retriever, get_provider(args.provider), gate, corpus.revision
    )
    by_id = corpus.by_id()
    provided = {}
    answer_rows = {r["id"]: r for r in answers["answers"]}
    for question in docs_questions:
        chunks = [by_id[key] for key in answer_rows[question["id"]]["context_chunks"]]
        relevant = set(question["relevant_sections"])
        provided[question["id"]] = {
            "chunks": len(chunks),
            "json_bytes": len(json.dumps([asdict(c) for c in chunks]).encode()),
            "section_recall": len({c.section_id for c in chunks} & relevant) / len(relevant),
        }
    context_stats = {
        "mean_chunks": _mean(r["chunks"] for r in provided.values()),
        "mean_json_bytes": _mean(r["json_bytes"] for r in provided.values()),
        "mean_section_recall": _mean(r["section_recall"] for r in provided.values()),
        "max_json_bytes": max((r["json_bytes"] for r in provided.values()), default=0),
    }
    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": {
            "revision": corpus.revision,
            "digest": corpus.digest,
            "chunker": CHUNKER_VERSION,
            "files": len(corpus.file_digests),
            "chunks": len(corpus.chunks),
            "sections": len(corpus.section_ids()),
        },
        "questions": {
            "file": questions_path.name,
            "sha256": hashlib.sha256(questions_path.read_bytes()).hexdigest(),
            "total": len(questions),
            "retrieval_scored": len(docs_questions),
            "by_route": dict(Counter(q["expected_route"] for q in questions)),
            "by_split": dict(Counter(q["split"] for q in questions)),
            "evaluated_split": args.split,
        },
        "environment": {
            "embedder": embedder.name,
            "python": platform.python_version(),
            "onnxruntime": _version("onnxruntime"),
            "tokenizers": _version("tokenizers"),
            "machine": platform.machine(),
            "backend": args.backend,
            "quality_signal": args.embedder == "bge",
        },
        "fusion": {"rrf_k": fusion_k, "depth": 50, "weights": [1.0, 1.0], "unit": "chunk"},
        "context": {
            "policy": CONTEXT_POLICY if expanded else "raw-five-chunks",
            "max_json_bytes": MAX_CONTEXT_BYTES if expanded else None,
            "ranking_metrics_use_expansion": False,
            "stats": context_stats,
            "per_question": provided,
        },
        "retrieval": retrieval,
        "comparisons": comparisons,
        "scope_gate": gate_report,
        "answers": answers,
    }


def summary(report: dict) -> str:
    lines = [
        f"Corpus {report['corpus']['revision'][:12]}: {report['corpus']['chunks']} chunks, "
        f"{report['corpus']['sections']} sections. Questions: {report['questions']['total']} "
        f"({report['questions']['retrieval_scored']} scored for retrieval).",
        "",
        "| Method | Split | n | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@10 | nDCG@5 | ms/query |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, result in report["retrieval"].items():
        for split in ("dev", "test"):
            s = result["by_split"][split]
            if not s["n"]:
                continue
            lines.append(
                f"| {name} | {split} | {s['n']} | {s['hit@1']:.2f} | {s['hit@3']:.2f} | "
                f"{s['hit@5']:.2f} | {s['recall@5']:.2f} | {s['mrr@10']:.2f} | {s['ndcg@5']:.2f} | "
                f"{result['latency_ms']['mean']:.1f} |"
            )
    lines += ["", "| Change | Metric | Mean difference | 95% CI |", "|---|---|---:|---|"]
    for pair, metrics in report["comparisons"].items():
        for metric, c in metrics.items():
            low, high = c["ci95"]
            lines.append(
                f"| {pair} | {metric} | {c['mean_difference']:+.3f} | [{low:+.3f}, {high:+.3f}] |"
            )
    gate, ans = report["scope_gate"], report["answers"]
    lines += [
        "",
        f"Scope gate: cosine >= {gate['threshold']} (dev accuracy {gate['dev_accuracy']:.2f}, "
        f"test {gate['test_accuracy'] if gate['test_accuracy'] is not None else 'not evaluated'}).",
        f"Routes (provider {ans['provider']}): {ans['route_accuracy']}. "
        f"Refusals: {ans['refusal']}.",
        f"Answered docs questions citing a relevant section: {ans['citation_hit_rate']:.2f} "
        f"of {ans['answered_docs_questions']}.",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--embedder", choices=("bge", "hashing"), default="bge")
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--context", choices=("expanded", "raw"), default="expanded")
    parser.add_argument("--fusion", choices=("legacy", "tuned"), default="tuned")
    parser.add_argument("--backend", choices=("postgres", "memory"), default="postgres")
    parser.add_argument("--setup-database", action="store_true")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--tracking-dir", default=str(ASSISTANT_ROOT / ".cache" / "mlflow"))
    parser.add_argument("--output", help="write the full JSON report here")
    parser.add_argument("--baseline", help="fail if development Recall@5 decreases")
    args = parser.parse_args(argv)
    report = run(args)
    from ledgerops_assistant.tracking import log_experiment

    report["mlflow_run_id"] = log_experiment(report, Path(args.tracking_dir))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(summary(report))
    if args.baseline:
        from ledgerops_assistant.quality_gate import check_regression

        check_regression(report, json.loads(Path(args.baseline).read_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
