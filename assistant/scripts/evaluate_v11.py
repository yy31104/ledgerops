"""Run the preregistered operations-v4 comparison once, using fake generation."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "assistant/eval/v11-holdout"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def verify(study=STUDY, root=ROOT):
    manifest = json.loads((study / "manifest.json").read_text())
    for name, digest in manifest["inputs_sha256"].items():
        if sha(root / name) != digest:
            raise ValueError(f"frozen input changed: {name}")
    for name, digest in (manifest["code_sha256"] | manifest.get("corpus_file_sha256", {})).items():
        if sha(root / name) != digest:
            raise ValueError(f"frozen implementation changed: {name}")
    return manifest


def verify_archive(study=STUDY, root=ROOT):
    """Check historical source bytes without running retrieval or pinning future code."""
    manifest = json.loads((study / "manifest.json").read_text())
    for name, digest in manifest["inputs_sha256"].items():
        if sha(root / name) != digest:
            raise ValueError(f"frozen input changed: {name}")
    commit = manifest["code_commit"]
    for name, digest in (manifest["code_sha256"] | manifest["corpus_file_sha256"]).items():
        original = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root)
        if hashlib.sha256(original).hexdigest() != digest:
            raise ValueError(f"archived source mismatch: {name}")
    return manifest


def choose_default(comparison):
    lower, _ = comparison["paired_section_cluster_ci95"]
    return "hybrid" if comparison["mean_difference"] > 0 and lower > 0 else "vector"


class Cached:
    """Cache identical calls, forwarding component metadata used by context selection."""

    def __init__(self, delegate):
        self.delegate, self.name, self.cache = delegate, delegate.name, {}

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def search(self, question, k=10):
        key = (question, k)
        if key not in self.cache:
            self.cache[key] = self.delegate.search(question, k)
        return self.cache[key]


def quote_coverage(chunks, question):
    evidence = question.get("evidence", [])
    if not evidence:
        return None
    found = sum(
        any(
            chunk.section_id == item["section_id"]
            and " ".join(item["quote"].split()) in " ".join(chunk.text.split())
            for chunk in chunks
        )
        for item in evidence
    )
    return found / len(evidence)


def run(output, study=STUDY, expected_commit=None):
    manifest = verify(study)
    split = manifest.get("split", "test")
    if split not in ("dev", "test"):
        raise ValueError("invalid evaluation split")
    if output.exists():
        raise FileExistsError("result output already exists")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if split == "test":
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
        from ledgerops_assistant.answer import ScopeGate
        from ledgerops_assistant.context import CONTEXT_POLICY, ContextRetriever
        from ledgerops_assistant.corpus import load_corpus
        from ledgerops_assistant.embeddings import BgeSmallEmbedder
        from ledgerops_assistant.evaluation import evaluate_answers, load_questions, score_ranking
        from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
        from ledgerops_assistant.providers.fake import FakeProvider
        from ledgerops_assistant.retrieval import HybridRetriever, rank_sections
        from ledgerops_assistant.tracking import log_experiment
        from scripts.evaluate_local import temporary_database
        from scripts.evaluate_v3 import bootstrap_summary

        corpus = load_corpus()
        if corpus.digest != manifest["corpus_digest"]:
            raise ValueError("corpus differs from the frozen source")
        questions = load_questions(study / "questions.jsonl", corpus.section_ids())
        docs = [q for q in questions if q["expected_route"] == "docs"]
        baseline = json.loads((ROOT / manifest["development_baseline"]).read_text())
        encoder, rows = BgeSmallEmbedder(), {}
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
            contexts = {name: ContextRetriever(method, corpus) for name, method in methods.items()}
            report["per_question"] = rows
            for q in docs:
                row = {"group": q["group"]}
                for name, method in methods.items():
                    hits = method.search(q["question"], 30)
                    chunks = contexts[name].context(q["question"], 5)
                    row[name] = {
                        **score_ranking(rank_sections(hits), q["relevant_sections"]),
                        "raw_five_chunk_coverage": score_ranking(
                            rank_sections(hits[:5]), q["relevant_sections"]
                        )["recall@5"],
                        "expanded_context_coverage": len(
                            {c.section_id for c in chunks} & set(q["relevant_sections"])
                        )
                        / len(q["relevant_sections"]),
                        "quoted_evidence_coverage": quote_coverage(chunks, q),
                        "ranked": rank_sections(hits),
                        "context_chunks": [c.chunk_id for c in chunks],
                    }
                rows[q["id"]] = row
                write(output, report)
            stats = bootstrap_summary(rows, tuple(methods), resamples=10000, seed=20261116)
            comparison = stats["comparisons"]["hybrid - pgvector_hnsw"]["recall@5"]
            selected = choose_default(comparison)
            selected_name = "pgvector_hnsw" if selected == "vector" else "hybrid"
            answers = evaluate_answers(
                questions,
                contexts[selected_name],
                FakeProvider(),
                ScopeGate(vector, baseline["scope_gate"]["threshold"]),
                corpus.revision,
            )
        answer_rows = {row["id"]: row for row in answers["answers"]}
        selected_method = "pgvector_hnsw" if selected == "vector" else "hybrid"
        delivered = [
            rows[q["id"]][selected_method]["expanded_context_coverage"]
            if answer_rows[q["id"]]["generation_called"]
            else 0.0
            for q in docs
        ]
        quoted_docs = [q for q in docs if q.get("evidence")]
        quote_summary = {
            name: {
                "questions_with_quotes": len(quoted_docs),
                "all_documentation_questions": len(docs),
                "quotes": sum(len(q["evidence"]) for q in quoted_docs),
                "mean_question_quote_coverage": sum(
                    rows[q["id"]][name]["quoted_evidence_coverage"] for q in quoted_docs
                )
                / len(quoted_docs)
                if quoted_docs
                else None,
            }
            for name in methods
        }
        report.update(
            corpus={
                "digest": corpus.digest,
                "revision": corpus.revision,
                "version": "operations-v4",
            },
            questions={
                "sha256": sha(study / "questions.jsonl"),
                "evaluated_split": split,
                "total": len(questions),
                "retrieval_scored": len(docs),
            },
            environment={"quality_signal": True, "backend": "postgres", "embedder": encoder.name},
            context={"policy": CONTEXT_POLICY, "max_json_bytes": 18000},
            statistics=stats,
            quote_coverage=quote_summary,
            generation_context={
                "method": selected,
                "calls": sum(answer_rows[q["id"]]["generation_called"] for q in docs),
                "questions": len(docs),
                "mean_delivered_section_coverage": sum(delivered) / len(delivered),
                "unsent_packs_count_as_delivered": False,
            },
            per_question=rows,
            answers=answers,
            decision={
                "method": selected,
                "rule": "hybrid only if paired section-cluster Recall@5 delta is positive "
                "and its 95% lower bound exceeds zero",
                "comparison": comparison,
            },
            scope_gate={
                "threshold": baseline["scope_gate"]["threshold"],
                "source": "development only",
            },
        )
        # The standard tracker records the complete evidence without remote services.
        report["retrieval"] = {
            name: {
                "by_split": {
                    split: {
                        "n": len(docs),
                        **{metric: value["mean"] for metric, value in metrics.items()},
                    }
                }
            }
            for name, metrics in stats["methods"].items()
        }
        report["mlflow_run_id"] = log_experiment(report, ROOT / "assistant/.cache/mlflow")
        report["status"] = record["status"] = "completed"
        write(output, report)
        record["result_sha256"] = sha(output)
        print(
            json.dumps(
                {
                    "decision": report["decision"],
                    "routes": answers["route_accuracy"],
                    "refusals": answers["refusal"],
                },
                indent=2,
            )
        )
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
    parser.add_argument("--output", type=Path, default=ROOT / "assistant/.cache/v11-holdout.json")
    parser.add_argument(
        "--freeze-commit", help="exact pushed source commit; required for held-out scoring"
    )
    args = parser.parse_args()
    raise SystemExit(run(args.output, expected_commit=args.freeze_commit))
