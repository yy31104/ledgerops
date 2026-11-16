"""One local subscription sample of all development documentation answers, never blind."""

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder
from ledgerops_assistant.evaluation import DEFAULT_QUESTIONS, load_questions
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
from ledgerops_assistant.providers import get_provider
from ledgerops_assistant.retrieval import HybridRetriever
from scripts.evaluate_local import temporary_database

ROOT = Path(__file__).resolve().parents[2]


def write(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    corpus = load_corpus()
    questions = [
        q
        for q in load_questions(DEFAULT_QUESTIONS, corpus.section_ids())
        if q["expected_route"] == "docs" and q["split"] == "dev"
    ]
    report = json.loads(args.development_report.read_text())
    if report["corpus"]["digest"] != corpus.digest:
        raise ValueError("development threshold report belongs to another corpus")
    hashes = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [*sorted((ROOT / "assistant/src").rglob("*.py")), Path(__file__)]
    }
    record = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "corpus_digest": corpus.digest,
        "code_sha256": hashes,
        "questions_sha256": hashlib.sha256(DEFAULT_QUESTIONS.read_bytes()).hexdigest(),
        "sampling": "All 32 development documentation questions, non-blind; no selection or tuning",
        "provider": "chatgpt_subscription",
        "billing": "subscription_quota",
        "model_selection": "Official SDK account default; no explicit model override",
        "billed_usd": None,
        "billed_usd_reason": "Subscription counters are quota usage, not an invoice",
        "retry_policy": "No automatic retries; failures remain in the record",
        "answers": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps(record, indent=2) + "\n")
    try:
        provider = get_provider("chatgpt_subscription")
        encoder = BgeSmallEmbedder()
        with temporary_database() as dsn:
            setup(dsn)
            key = index_corpus(dsn, corpus, encoder)
            vector = PostgresRetriever(dsn, key, encoder, method="vector")
            retriever = ContextRetriever(
                HybridRetriever(PostgresRetriever(dsn, key, encoder, method="fts"), vector), corpus
            )
            gate = ScopeGate(vector, report["scope_gate"]["threshold"])
            for q in questions:
                started = time.perf_counter()
                row = {"id": q["id"], "question": q["question"], "key_facts": q["key_facts"]}
                try:
                    row["result"] = answer(
                        q["question"], retriever, provider, gate=gate, revision=corpus.revision
                    ).to_dict()
                except Exception as error:
                    row["error_type"] = type(error).__name__
                row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                record["answers"].append(row)
                write(args.output, record)
                print(f"{q['id']}: {'recorded' if 'result' in row else 'failed'}", flush=True)
        record["status"] = (
            "completed" if all("result" in r for r in record["answers"]) else "partial"
        )
    except BaseException as error:
        record["status"] = "failed"
        record["error_type"] = type(error).__name__
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        write(args.output, record)


if __name__ == "__main__":
    main()
