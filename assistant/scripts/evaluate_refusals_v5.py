"""Run the frozen independent refusal sample once with fake generation."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ledgerops_assistant.answer import ScopeGate
from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder
from ledgerops_assistant.evaluation import evaluate_answers, load_questions
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
from ledgerops_assistant.providers.fake import FakeProvider
from ledgerops_assistant.retrieval import HybridRetriever
from scripts.evaluate_local import temporary_database

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "assistant/eval/refusal-v5"


def main():
    manifest = json.loads((STUDY / "manifest.json").read_text())
    if (
        hashlib.sha256((STUDY / "protocol.json").read_bytes()).hexdigest()
        != manifest["protocol_sha256"]
    ):
        raise ValueError("protocol changed after freeze")
    baseline_path = ROOT / "assistant/eval/baselines/operations-v4.json"
    if hashlib.sha256(baseline_path.read_bytes()).hexdigest() != manifest["baseline_sha256"]:
        raise ValueError("development baseline changed after freeze")
    questions_path = STUDY / "questions.jsonl"
    if hashlib.sha256(questions_path.read_bytes()).hexdigest() != manifest["questions_sha256"]:
        raise ValueError("questions changed after freeze")
    for name, digest in manifest["code_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError("code changed after freeze")
    record = {"status": "running", "started_at": datetime.now(UTC).isoformat()}
    run = STUDY / "run.json"
    with run.open("x") as output:
        output.write(json.dumps(record) + "\n")
    try:
        corpus = load_corpus()
        if corpus.digest != manifest["corpus_digest"]:
            raise ValueError("corpus changed after freeze")
        questions = load_questions(questions_path, corpus.section_ids())
        baseline = json.loads((ROOT / "assistant/eval/baselines/operations-v4.json").read_text())
        encoder = BgeSmallEmbedder()
        with temporary_database() as dsn:
            setup(dsn)
            key = index_corpus(dsn, corpus, encoder)
            vector = PostgresRetriever(dsn, key, encoder, method="vector")
            retriever = ContextRetriever(
                HybridRetriever(PostgresRetriever(dsn, key, encoder, method="fts"), vector), corpus
            )
            answers = evaluate_answers(
                questions,
                retriever,
                FakeProvider(),
                ScopeGate(vector, baseline["scope_gate"]["threshold"]),
                corpus.revision,
            )
        result = {
            "manifest_sha256": hashlib.sha256((STUDY / "manifest.json").read_bytes()).hexdigest(),
            "corpus_digest": corpus.digest,
            "answers": answers,
        }
        path = ROOT / "assistant/.cache/refusal-v5.json"
        with path.open("x") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        record["status"] = "completed"
        record["result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        print(
            json.dumps({key: value for key, value in answers.items() if key != "answers"}, indent=2)
        )
    except BaseException as error:
        record["status"], record["error_type"] = "failed", type(error).__name__
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        run.write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
