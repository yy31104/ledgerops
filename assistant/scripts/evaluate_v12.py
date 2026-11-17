"""Run the frozen v1.2 scope study once, using fake generation.

New out-of-scope questions measure how many still pass the scope gate; new
documentation questions measure false refusals and retrieval. The released v1.1
gate is scored on the same questions for comparison.
"""

import argparse
import json
import math
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.evaluate_v11 import Cached, quote_coverage, sha, verify, write

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "assistant/eval/v12-holdout"
SEED = 20261117


def wilson(successes, total, z=1.959964):
    """95% Wilson score interval for a proportion."""
    if not total:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def rate(rows, expected, key, wrong):
    """How many rows with this expected route got a wrong decision under ``key``."""
    subset = [r for r in rows if r["expected_route"] == expected]
    misses = [r["id"] for r in subset if wrong(r[key])]
    return {
        "n": len(subset),
        "count": len(misses),
        "rate": round(len(misses) / len(subset), 4) if subset else None,
        "wilson_ci95": wilson(len(misses), len(subset)),
        "ids": misses,
    }


def outcome_summary(rows, key):
    return {
        "out_of_scope_passed": rate(rows, "out_of_scope", key, lambda d: d == "docs"),
        "documentation_refused": rate(rows, "docs", key, lambda d: d != "docs"),
        "route_accuracy": round(sum(r[key] == r["expected_route"] for r in rows) / len(rows), 4),
        "out_of_scope_passed_by_category": {
            category: sum(r[key] == "docs" for r in rows if r.get("category") == category)
            for category in sorted(
                {r["category"] for r in rows if r["expected_route"] == "out_of_scope"}
            )
        },
    }


def refusal_stage(question, routing, coverage, similarity, threshold):
    """First check that stops a question before generation, in the order of ``answer``."""
    route = routing.classify(question)
    if route == routing.ACTION:
        return "routing"
    if routing.unsupported_topic(question):
        return "named_capability_list"
    if route == routing.LIVE_STATE:
        return "routing"
    measured = coverage.measure(question)
    if measured.unknown_codes:
        return "unknown_code"
    if measured.uncovered_share >= coverage.threshold:
        return "uncovered_share"
    if similarity < threshold:
        return "similarity"
    return None


def run(output, study=STUDY, expected_commit=None):
    manifest = verify(study)
    if output.exists():
        raise FileExistsError("result output already exists")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if manifest.get("split", "test") == "test":
        if not expected_commit or expected_commit != head:
            raise ValueError("supply the exact pushed freeze commit")
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
        )
        if dirty.strip():
            raise ValueError("the frozen checkout has tracked changes")
    record = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "freeze_commit": head,
        "manifest_sha256": sha(study / "manifest.json"),
    }
    run_path = study / "run.json"
    with run_path.open("x") as file:
        file.write(json.dumps(record, indent=2) + "\n")
    report = dict(record)
    claimed_output = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as file:
            file.write(json.dumps(report, indent=2) + "\n")
        claimed_output = True
        # Import/setup only after the exclusive attempt has been claimed.
        from ledgerops_assistant import routing
        from ledgerops_assistant.answer import ScopeGate
        from ledgerops_assistant.context import CONTEXT_POLICY, ContextRetriever
        from ledgerops_assistant.corpus import load_corpus
        from ledgerops_assistant.coverage import DocumentCoverage
        from ledgerops_assistant.embeddings import BgeSmallEmbedder
        from ledgerops_assistant.evaluation import evaluate_answers, load_questions, score_ranking
        from ledgerops_assistant.policy import coverage_threshold, scope_threshold
        from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
        from ledgerops_assistant.providers.fake import FakeProvider
        from ledgerops_assistant.retrieval import HybridRetriever, rank_sections
        from scripts.evaluate_local import temporary_database
        from scripts.evaluate_v3 import bootstrap_summary
        from scripts.scope_dev import v11_decision, v11_routing

        corpus = load_corpus()
        if corpus.digest != manifest["corpus_digest"]:
            raise ValueError("corpus differs from the frozen source")
        baseline = json.loads((ROOT / manifest["development_baseline"]).read_text())
        threshold, share = scope_threshold(), coverage_threshold()
        if (threshold, share) != (
            baseline["scope_gate"]["threshold"],
            baseline["scope_gate"]["coverage_threshold"],
        ):
            raise ValueError("release thresholds differ from the development baseline")
        questions = load_questions(study / "questions.jsonl", corpus.section_ids())
        docs = [q for q in questions if q["expected_route"] == "docs"]
        coverage = DocumentCoverage(corpus, share)
        old = v11_routing()
        encoder, rows, decisions = BgeSmallEmbedder(), {}, []
        with temporary_database() as dsn:
            setup(dsn)
            key = index_corpus(dsn, corpus, encoder)
            vector = Cached(PostgresRetriever(dsn, key, encoder, method="vector"))
            fts = Cached(PostgresRetriever(dsn, key, encoder, method="fts"))
            methods = {
                "postgres_fts": fts,
                "pgvector_hnsw": vector,
                "hybrid": Cached(HybridRetriever(fts, vector, rrf_k=10)),
                "legacy_hybrid": Cached(HybridRetriever(fts, vector, rrf_k=60)),
            }
            contexts = {name: ContextRetriever(m, corpus) for name, m in methods.items()}
            context = contexts["hybrid"]
            for q in docs:
                relevant = q["relevant_sections"]
                row = {"group": q["group"]}
                for name, method in methods.items():
                    hits = method.search(q["question"], 30)
                    chunks = contexts[name].context(q["question"], 5)
                    row[name] = {
                        **score_ranking(rank_sections(hits), relevant),
                        "raw_five_chunk_coverage": score_ranking(rank_sections(hits[:5]), relevant)[
                            "recall@5"
                        ],
                        "expanded_context_coverage": len(
                            {c.section_id for c in chunks} & set(relevant)
                        )
                        / len(relevant),
                        "quoted_evidence_coverage": quote_coverage(chunks, q),
                    }
                rows[q["id"]] = row
            answers = evaluate_answers(
                questions,
                context,
                FakeProvider(),
                ScopeGate(vector, threshold, coverage=coverage),
                corpus.revision,
            )
            predicted = {r["id"]: r["predicted_route"] for r in answers["answers"]}
            for q in questions:
                hits = vector.search(q["question"], 1)
                similarity = hits[0].score if hits else 0.0
                measured = coverage.measure(q["question"])
                decisions.append(
                    {
                        "id": q["id"],
                        "category": q["category"],
                        "expected_route": q["expected_route"],
                        "v12": predicted[q["id"]],
                        "v11": v11_decision(old, q["question"], similarity),
                        "v12_refusal_stage": refusal_stage(
                            q["question"], routing, coverage, similarity, threshold
                        ),
                        "similarity": round(similarity, 4),
                        "uncovered_share": measured.uncovered_share,
                        "unknown_codes": list(measured.unknown_codes),
                    }
                )
        stats = bootstrap_summary(rows, tuple(methods), resamples=10000, seed=SEED)
        quoted = [q for q in docs if q.get("evidence")]
        report.update(
            corpus={"digest": corpus.digest, "revision": corpus.revision},
            questions={
                "sha256": sha(study / "questions.jsonl"),
                "total": len(questions),
                "by_route": dict(Counter(q["expected_route"] for q in questions)),
                "by_category": dict(Counter(q["category"] for q in questions)),
            },
            environment={"quality_signal": True, "backend": "postgres", "embedder": encoder.name},
            thresholds={"similarity": threshold, "coverage_share": share},
            context={"policy": CONTEXT_POLICY, "retrieval": "hybrid", "max_json_bytes": 18000},
            outcomes={
                "v1.2": outcome_summary(decisions, "v12"),
                "v1.1": outcome_summary(decisions, "v11"),
            },
            v12_refusal_stages=dict(
                Counter(d["v12_refusal_stage"] for d in decisions if d["v12"] != "docs")
            ),
            retrieval=stats,
            context_coverage={
                "mean_expanded_section_coverage": sum(
                    r["hybrid"]["expanded_context_coverage"] for r in rows.values()
                )
                / len(rows),
                "mean_quote_coverage": {
                    name: sum(rows[q["id"]][name]["quoted_evidence_coverage"] for q in quoted)
                    / len(quoted)
                    for name in methods
                }
                if quoted
                else None,
                "questions_with_quotes": len(quoted),
            },
            decisions=decisions,
            per_question=rows,
            answers=answers,
        )
        report["status"] = record["status"] = "completed"
        write(output, report)
        record["result_sha256"] = sha(output)
        print(json.dumps(report["outcomes"], indent=2))
        return 0
    except BaseException as error:
        record["status"] = report["status"] = "failed"
        record["error_type"] = report["error_type"] = type(error).__name__
        if claimed_output:
            write(output, report)
        print(f"Evaluation failed: {type(error).__name__}")
        return 1
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        write(run_path, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "assistant/.cache/v12-holdout.json")
    parser.add_argument("--freeze-commit", help="exact pushed source commit; required")
    args = parser.parse_args()
    raise SystemExit(run(args.output, expected_commit=args.freeze_commit))
