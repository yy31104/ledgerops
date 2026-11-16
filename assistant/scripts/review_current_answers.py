"""Record the original 32 documentation answers once, for pending human review."""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.context import CONTEXT_POLICY, ContextRetriever
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.embeddings import BgeSmallEmbedder
from ledgerops_assistant.evaluation import load_questions
from ledgerops_assistant.policy import default_retrieval
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
from ledgerops_assistant.providers.base import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    parse_draft,
    user_message,
)
from ledgerops_assistant.providers.subscription import SubscriptionProvider
from ledgerops_assistant.retrieval import HybridRetriever
from scripts.evaluate_local import temporary_database

ROOT = Path(__file__).resolve().parents[2]
QUESTIONS = ROOT / "assistant/eval/v4-dev/questions.jsonl"
POLICY = ROOT / "assistant/src/ledgerops_assistant/retrieval_policy.json"
MODEL = "gpt-6.1-sol"
COHORT_IDS = [f"v1-{i:03}" for i in range(1, 33)]


def now():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, record):
    """Replace only our claimed record, using an owner-private temporary file."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        json.dump(record, temporary, indent=2, allow_nan=False)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def original_questions(sections):
    manifest = json.loads(QUESTIONS.with_name("manifest.json").read_text())
    if sha(QUESTIONS) != manifest["questions_sha256"]:
        raise ValueError("original questions no longer match their versioned manifest")
    questions = [q for q in load_questions(QUESTIONS, sections) if q["expected_route"] == "docs"]
    if [q["id"] for q in questions] != COHORT_IDS:
        raise ValueError("the review cohort must be the original 32 documentation questions")
    return questions


def payload_sizes(question, passages):
    empty = len(user_message("", []).encode())
    return {
        "context_utf8_bytes": len(user_message("", passages).encode()) - empty,
        "question_utf8_bytes": len(question.encode()),
        "instruction_utf8_bytes": len(SYSTEM_PROMPT.encode()),
        "message_framing_utf8_bytes": empty,
        "message_utf8_bytes": len(user_message(question, passages).encode()),
        "schema_json_utf8_bytes": len(json.dumps(ANSWER_SCHEMA).encode()),
    }


class CaptureProvider:
    """Capture the exact source passages passed to the answer provider."""

    def __init__(self, provider, row, persist):
        self.provider, self.row, self.name, self.persist = provider, row, provider.name, persist

    def draft(self, question, passages):
        self.row["provided_sources"] = [asdict(chunk) for chunk in passages]
        self.row["payload"] = payload_sizes(question, passages)
        self.row["generation_status"] = "attempted"
        self.persist()
        started = time.perf_counter()
        try:
            raw, usage = self.provider.generate(
                SYSTEM_PROMPT, user_message(question, passages), ANSWER_SCHEMA
            )
            self.row["generation"] = {"raw_response": raw, "usage": usage}
            self.persist()
            return parse_draft(raw, usage)
        finally:
            self.row["provider_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)


def token_contrast(record):
    """A measured empty-input contrast, never an exact SDK/component allocation."""

    def count(usage):
        value = usage.get("input_tokens")
        return value if type(value) is int and value >= 0 else None

    baseline = count(record["calibration"].get("usage", {}))
    values = [count(row.get("generation", {}).get("usage", {})) for row in record["answers"]]
    known = [value for value in values if value is not None]
    complete = baseline is not None and len(known) == len(COHORT_IDS)
    mean = statistics.mean(known) if known else None
    return {
        "empty_evidence_input_tokens": baseline,
        "reported_full_calls": len(known),
        "planned_full_calls": len(COHORT_IDS),
        "mean_reported_full_input_tokens": mean,
        "complete": complete,
        "mean_full_minus_empty_input_tokens": mean - baseline if complete else None,
        "interpretation": (
            "Measured contrast between separate threads with the same model, instructions and "
            "schema. Empty input includes SDK/system framing, instructions and schema. "
            "Full minus empty includes evidence, question and possible framing variation; "
            "the SDK does not expose an exact component-token allocation."
        ),
    }


def collect_review(output, questions, metadata, session_factory):
    """Claim before setup; preserve failed attempts and never retry a generation."""
    if [q["id"] for q in questions] != COHORT_IDS:
        raise ValueError("review requires the exact original 32-question cohort")
    record = {
        **metadata,
        "status": "running",
        "started_at": now(),
        "requested_model": MODEL,
        "billing": "subscription_quota",
        "billed_usd": None,
        "human_review": {"status": "pending", "allowed": ["complete", "incomplete", "incorrect"]},
        "retry_policy": "One attempt per call; no automatic retries or resumed generation",
        "calibration": {"status": "not_started", "cohort_member": False},
        "answers": [
            {
                "id": q["id"],
                "question": q["question"],
                "key_facts": q["key_facts"],
                "status": "not_started",
                "provided_sources": [],
                "judgement": "",
            }
            for q in questions
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
    try:
        with session_factory() as (provider, retriever, gate):
            if provider.name != "chatgpt_subscription" or provider.model != MODEL:
                raise ValueError("human review requires the pinned local subscription adapter")
            calibration = record["calibration"]
            calibration.update(status="running", started_at=now(), payload=payload_sizes("", []))
            write(output, record)
            started = time.perf_counter()
            try:
                raw, usage = provider.generate(SYSTEM_PROMPT, user_message("", []), ANSWER_SCHEMA)
                calibration.update(status="recorded", raw_response=raw, usage=usage)
            except Exception as error:
                calibration.update(status="error", error_type=type(error).__name__)
            except BaseException as error:
                calibration.update(status="interrupted", error_type=type(error).__name__)
                raise
            finally:
                calibration["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                write(output, record)
            for row in record["answers"]:
                row.update(status="running", started_at=now(), generation_status="not_called")
                write(output, record)
                started = time.perf_counter()
                try:
                    row["result"] = answer(
                        row["question"],
                        retriever,
                        CaptureProvider(provider, row, lambda: write(output, record)),
                        gate=gate,
                        revision=record["corpus_revision"],
                    ).to_dict()
                    row["status"] = "recorded"
                except Exception as error:
                    row.update(status="error", error_type=type(error).__name__)
                except BaseException as error:
                    row.update(status="interrupted", error_type=type(error).__name__)
                    raise
                finally:
                    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    write(output, record)
                print(f"{row['id']}: {row['status']}", flush=True)
        record["status"] = (
            "completed"
            if calibration["status"] == "recorded"
            and all(row["status"] == "recorded" for row in record["answers"])
            else "partial"
        )
    except BaseException as error:
        record.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        record["finished_at"] = now()
        record["token_contrast"] = token_contrast(record)
        write(output, record)
    return record


@contextmanager
def subscription_session(corpus, method, threshold):
    provider = SubscriptionProvider(model=MODEL)
    encoder = BgeSmallEmbedder()
    with temporary_database() as dsn:
        setup(dsn)
        key = index_corpus(dsn, corpus, encoder)
        vector = PostgresRetriever(dsn, key, encoder, method="vector")
        selected = (
            vector
            if method == "vector"
            else HybridRetriever(PostgresRetriever(dsn, key, encoder, method="fts"), vector)
        )
        yield provider, ContextRetriever(selected, corpus), ScopeGate(vector, threshold)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    corpus = load_corpus()
    if not re.fullmatch(r"[0-9a-f]{40}", corpus.revision):
        raise ValueError("human review requires a committed corpus snapshot")
    questions = original_questions(corpus.section_ids())
    report = json.loads(args.development_report.read_text())
    policy = json.loads(POLICY.read_text())
    method, threshold = default_retrieval(), report["scope_gate"]["threshold"]
    if report["corpus"]["digest"] != corpus.digest:
        raise ValueError("development report belongs to another corpus")
    if (
        not report["environment"]["quality_signal"]
        or report["environment"]["backend"] != "postgres"
    ):
        raise ValueError("human review requires a real PostgreSQL/BGE development report")
    if report["context"]["retrieval_method"] != method or threshold != policy["scope_threshold"]:
        raise ValueError("development report and release runtime policy differ")
    scripts = [Path(__file__), Path(__file__).with_name("export_human_review.py")]
    metadata = {
        "schema_version": 2,
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "corpus_digest": corpus.digest,
        "corpus_revision": corpus.revision,
        "context_policy": CONTEXT_POLICY,
        "embedder": report["environment"]["embedder"],
        "retrieval_method": method,
        "retrieval_policy": policy,
        "retrieval_policy_sha256": sha(POLICY),
        "questions_file": "eval/v4-dev/questions.jsonl",
        "questions_sha256": sha(QUESTIONS),
        "development_report_sha256": sha(args.development_report),
        "cohort": "All original 32 development documentation questions; non-blind",
        "code_sha256": {
            p.relative_to(ROOT).as_posix(): sha(p)
            for p in [*sorted((ROOT / "assistant/src").rglob("*.py")), *scripts]
        },
    }
    collect_review(
        args.output,
        questions,
        metadata,
        lambda: subscription_session(corpus, method, threshold),
    )


if __name__ == "__main__":
    main()
