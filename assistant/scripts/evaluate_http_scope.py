"""Run the frozen scope comparison over authenticated local HTTP and SSE."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from ledgerops_assistant import routing
from ledgerops_assistant.corpus import CORPUS_FILES, load_corpus
from ledgerops_assistant.demo_api import Handler
from ledgerops_assistant.embeddings import BgeSmallEmbedder
from ledgerops_assistant.evaluation import score_ranking
from ledgerops_assistant.postgres import index_corpus, setup
from ledgerops_assistant.retrieval import rank_sections
from ledgerops_assistant.runtime import retriever_for
from ledgerops_assistant.settings import Settings
from scripts.evaluate_local import temporary_database
from scripts.evaluate_v11 import sha, write
from scripts.evaluate_v13 import git, paired_recall, rates

ROOT = Path(__file__).resolve().parents[2]
STUDY = ROOT / "assistant/eval/http-scope"
PROTOCOL_COMMIT = "c27460ee00b32717e08a8c7017de632e65323658"
SERVER_KEYS = ("CI", "LEDGEROPS_ASSISTANT_SERVER", "OPENAI_API_KEY", "OPENAI_BASE_URL")
RANKING_FILES = (
    "context.py",
    "corpus.py",
    "embeddings.py",
    "postgres.py",
    "retrieval.py",
    "sql/001_rag.sql",
)
MODEL = "gpt-6.1-sol"
CONCURRENCY = 3
FAILED_REQUESTS = {
    "authentication_required",
    "cancelled",
    "deadline_exceeded",
    "forbidden",
    "invalid_response",
    "model_or_budget_error",
    "not_found_or_hidden",
    "passage_verifier_unavailable",
    "rate_limited",
    "response_too_large",
    "timeout",
    "upstream_unavailable",
}
QUESTION_FIELDS = {
    "id",
    "split",
    "expected_route",
    "category",
    "group",
    "question",
    "relevant_sections",
    "key_facts",
    "evidence",
    "source_chunk",
}


def protocol():
    path = STUDY / "protocol.json"
    if path.read_bytes() != git("show", f"{PROTOCOL_COMMIT}:{path.relative_to(ROOT)}"):
        raise ValueError("the registered HTTP protocol was changed")
    return json.loads(path.read_text())


def rows_at(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tracked_inputs():
    files = git("ls-files", "-z", "--", "assistant/src", "assistant/scripts").decode().split("\0")
    return sorted(
        {name for name in files if name}
        | set(CORPUS_FILES)
        | {
            "assistant/eval/http-scope/audit-summary.json",
            "assistant/eval/http-scope/development.json",
            "assistant/eval/http-scope/questions.jsonl",
            "assistant/eval/http-scope/protocol.json",
            "assistant/pyproject.toml",
            "assistant/uv.lock",
            "pyproject.toml",
            "uv.lock",
        }
    )


def validate_cohort(questions, spec, corpus, audit):
    chunks = corpus.by_id()
    sections = corpus.section_ids()
    for q in questions:
        if (
            set(q) != QUESTION_FIELDS
            or q["split"] != "test"
            or not isinstance(q["id"], str)
            or not q["id"]
            or not isinstance(q["question"], str)
            or not q["question"].strip()
        ):
            raise ValueError("invalid holdout question schema")
        if q["expected_route"] == "docs":
            source = chunks.get(q["source_chunk"])
            relevant = set(q["relevant_sections"])
            facts = q["key_facts"]
            evidence = q["evidence"]
            if (
                q["category"] != "documentation"
                or source is None
                or q["group"] != source.section_id
                or q["group"] not in relevant
                or relevant - sections
                or not 1 <= len(facts) <= 2
                or {item["fact_index"] for item in evidence} != set(range(len(facts)))
                or {item["section_id"] for item in evidence} != relevant
            ):
                raise ValueError("invalid documentation labels")
        elif (
            q["expected_route"] != "out_of_scope"
            or q["category"] not in {"product_gap", "unrelated"}
            or q["group"] != q["category"]
            or q["source_chunk"] is not None
            or any(q[field] != [] for field in ("relevant_sections", "key_facts", "evidence"))
        ):
            raise ValueError("invalid unsupported labels")

    counts = Counter(row["expected_route"] for row in questions)
    gaps = sum(
        row["expected_route"] == "out_of_scope" and row["category"] == "product_gap"
        for row in questions
    )
    targets = spec["authoring"]
    if (
        len(questions) != len({row["id"] for row in questions})
        or audit["documentation"] != counts["docs"]
        or audit["out_of_scope"] != counts["out_of_scope"]
        or audit["product_gaps"] != gaps
        or counts["docs"] < targets["minimum_documentation"]
        or counts["out_of_scope"] < targets["minimum_out_of_scope"]
        or gaps / counts["out_of_scope"] < targets["minimum_product_gap_share"]
    ):
        raise ValueError("invalid frozen cohort")


def frozen_questions(expected_commit, spec, corpus):
    head = git("rev-parse", "HEAD").decode().strip()
    if head != expected_commit or git("status", "--porcelain"):
        raise ValueError("run from the exact freeze commit with a clean worktree")
    ref = git("rev-parse", "--symbolic-full-name", "@{upstream}").decode().strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix):
        raise ValueError("the freeze branch must track origin")
    remote = "refs/heads/" + ref.removeprefix(prefix)
    if git("ls-remote", "--exit-code", "origin", remote).decode().split() != [head, remote]:
        raise ValueError("origin does not point to the freeze commit")

    manifest = json.loads((STUDY / "manifest.json").read_text())
    pinned = manifest["sha256"]
    if sorted(pinned) != tracked_inputs() or any(
        sha(ROOT / path) != digest for path, digest in pinned.items()
    ):
        raise ValueError("the freeze does not match the tracked trial inputs")
    if manifest["corpus_digest"] != corpus.digest or manifest["protocol_commit"] != PROTOCOL_COMMIT:
        raise ValueError("invalid freeze corpus or protocol")
    question_file = STUDY / "questions.jsonl"
    audit = json.loads((STUDY / "audit-summary.json").read_text())
    if audit["questions_sha256"] != sha(question_file):
        raise ValueError("the audit does not match the frozen questions")
    questions = rows_at(question_file)
    validate_cohort(questions, spec, corpus, audit["retained"])
    return questions


@contextmanager
def reference_checkout(commit):
    with TemporaryDirectory(prefix="ledgerops-http-reference-") as directory:
        target = Path(directory)
        archive = target / "reference.tar"
        with archive.open("wb") as output:
            subprocess.run(["git", "archive", commit], cwd=ROOT, stdout=output, check=True)
        with tarfile.open(archive) as source:
            source.extractall(target, filter="data")
        yield target


def check_ranks(reference):
    source = ROOT / "assistant/src/ledgerops_assistant"
    for name in RANKING_FILES:
        relative = (source / name).relative_to(ROOT)
        if (ROOT / relative).read_bytes() != git("show", f"{reference}:{relative}"):
            raise ValueError(f"ranking code differs from the v1.2 reference: {name}")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def synthetic_api(token):
    previous = os.environ.get("LEDGEROPS_ASSISTANT_DEMO_TOKEN")
    os.environ["LEDGEROPS_ASSISTANT_DEMO_TOKEN"] = token
    try:
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                thread.join(timeout=5)
    finally:
        if previous is None:
            del os.environ["LEDGEROPS_ASSISTANT_DEMO_TOKEN"]
        else:
            os.environ["LEDGEROPS_ASSISTANT_DEMO_TOKEN"] = previous


@contextmanager
def serve(root, env, log):
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with (
        log.open("x") as output,
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ledgerops_assistant.runtime",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env={**env, "PYTHONPATH": str(root / "assistant/src")},
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as process,
    ):
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                for _ in range(120):
                    if process.poll() is not None:
                        break
                    try:
                        if client.get(origin + "/health/ready").status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("assistant readiness timed out")
            if process.poll() is not None:
                raise RuntimeError(f"assistant exited during startup, see {log.name}")
            yield origin
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def ask(client, origin, question):
    start = time.perf_counter()
    terminal = None
    error = None
    event = None
    with client.stream(
        "POST",
        origin + "/v1/assist",
        json={"question": question, "request_id": str(uuid4()), "provider": "fake"},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                if event == "result":
                    if terminal is not None:
                        raise ValueError("duplicate SSE result")
                    terminal = json.loads(line[6:])
                elif event == "error":
                    error = json.loads(line[6:])["reason"]
            elif not line:
                event = None
    if error or terminal is None:
        raise ValueError(f"HTTP assistant stream failed: {error or 'missing_result'}")
    answer = terminal["answer"]
    if type(answer["refused"]) is not bool:
        raise ValueError("invalid HTTP refusal")
    return {
        "admitted": not answer["refused"],
        "reason": answer["reason"],
        "route": terminal.get("route"),
        "steps": terminal["steps"],
        "duration_ms": terminal["duration_ms"],
        "http_ms": round(1000 * (time.perf_counter() - start), 2),
        "verification": terminal.get("verification"),
        "citations": [citation["chunk_id"] for citation in answer["citations"]],
    }


def request_pair(reference, candidate, headers, question):
    with httpx.Client(headers=headers, timeout=135, trust_env=False) as client:
        return ask(client, reference, question), ask(client, candidate, question)


def valid_candidate(response, model):
    route = response["route"]
    if route not in {routing.ACTION, routing.DOCS, routing.LIVE_STATE}:
        return False
    verification = response["verification"]
    if verification is None:
        return not response["admitted"] or route == routing.LIVE_STATE
    if route != routing.DOCS or not verification.get("passage_ids"):
        return False
    status = verification["status"]
    legal_status = {"supported", "unsupported", "invalid"}
    usage = verification.get("usage", {})
    return (
        status in legal_status
        and (not response["admitted"] or status == "supported")
        and (usage.get("model"), usage.get("billing")) == (model, "chatgpt_plan")
    )


def score_trial(questions, spec, corpus, output, report):
    check_ranks(spec["reference_retrieval_commit"])
    if spec["candidate"]["verifier_model"] != MODEL:
        raise ValueError("trial verifier model differs from the preregistered model")
    if any(os.environ.get(key) for key in SERVER_KEYS):
        raise ValueError("run the local subscription study outside CI and without paid API config")
    if os.environ.get("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION") != "1":
        raise ValueError("enable the local ChatGPT-plan adapter before the run")
    token = "lo1_" + secrets.token_urlsafe(32)
    headers = {"Authorization": "Bearer " + token}
    with ExitStack() as stack:
        dsn = stack.enter_context(temporary_database())
        setup(dsn)
        index_corpus(dsn, corpus, BgeSmallEmbedder())
        api = stack.enter_context(synthetic_api(token))
        reference = stack.enter_context(reference_checkout(spec["reference_http_commit"]))
        env = {key: val for key, val in os.environ.items() if not key.startswith("LEDGEROPS_")}
        env.update(
            PYTHONDONTWRITEBYTECODE="1",
            LEDGEROPS_ASSISTANT_API_ORIGIN=api,
            LEDGEROPS_ASSISTANT_DATABASE_URL=dsn,
            LEDGEROPS_ASSISTANT_PROVIDERS="fake",
            LEDGEROPS_ASSISTANT_EMBEDDER="bge",
            LEDGEROPS_ASSISTANT_RETRIEVAL="hybrid",
        )
        old = stack.enter_context(serve(reference, env, output / "reference.log"))
        new = stack.enter_context(
            serve(
                ROOT,
                {
                    **env,
                    "LEDGEROPS_ASSISTANT_SCOPE_VERIFIER": "subscription",
                    "LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION": "1",
                    "LEDGEROPS_ASSISTANT_DEADLINE_SECONDS": "120",
                },
                output / "candidate.log",
            )
        )
        # Both arms and the frozen v1.2 reference use byte-identical raw rankers.
        ranker = retriever_for(Settings(database_url=dsn, retrieval="hybrid"))
        rows = report["rows"]
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for offset in range(0, len(questions), CONCURRENCY):
                batch = questions[offset : offset + CONCURRENCY]
                futures = [
                    pool.submit(request_pair, old, new, headers, q["question"]) for q in batch
                ]
                errors = []
                for q, future in zip(batch, futures, strict=True):
                    try:
                        baseline, candidate = future.result()
                    except (ValueError, httpx.HTTPError) as error:
                        errors.append({"id": q["id"], "error": type(error).__name__})
                        continue
                    row = {
                        "id": q["id"],
                        "expected_route": q["expected_route"],
                        "category": q["category"],
                        "group": q["group"],
                        "reference_admitted": baseline["admitted"],
                        "candidate_admitted": candidate["admitted"],
                        "reference_http": baseline,
                        "candidate_http": candidate,
                    }
                    if q["expected_route"] == "docs":
                        ranked = rank_sections(ranker.search(q["question"], 30))
                        recall = score_ranking(ranked, q["relevant_sections"])["recall@5"]
                        row.update(
                            candidate_recall=recall, reference_recall=recall, ranked_sections=ranked
                        )
                    if {baseline["reason"], candidate["reason"]} & FAILED_REQUESTS:
                        errors.append({"id": q["id"], "error": "service_failure"})
                    if not valid_candidate(candidate, MODEL):
                        errors.append({"id": q["id"], "error": "verifier_contract"})
                    rows.append(row)
                if errors:
                    report["errors"].extend(errors)
                write(output / "report.json", report)
                print(f"{len(rows)}/{len(questions)}", flush=True)
                if errors:
                    raise RuntimeError("HTTP transport or verifier failed; preserved partial rows")


def summarize(report, spec):
    from statistics import median

    rows = report["rows"]
    reference = rates(rows, "reference_admitted")
    candidate = rates(rows, "candidate_admitted")
    retrieval = paired_recall(
        [row for row in rows if row["expected_route"] == "docs"],
        seed=spec["measurement"]["bootstrap_seed"],
        resamples=spec["measurement"]["bootstrap_resamples"],
    )
    bounds = spec["targets"]
    checks = {
        "out_of_scope": (
            candidate["out_of_scope_passed"]["rate"] <= bounds["out_of_scope_pass_rate_max"]
        ),
        "documentation": (
            candidate["documentation_refused"]["rate"]
            <= bounds["documentation_false_refusal_rate_max"]
        ),
        "retrieval": (
            retrieval["section_cluster_ci95"][0] >= bounds["paired_recall_at_5_ci_lower_min"]
        ),
    }
    calls = [v for row in rows if (v := row["candidate_http"]["verification"]) and v.get("usage")]
    return {
        "reference": reference,
        "candidate": candidate,
        "retrieval": retrieval,
        "targets": checks,
        "passed": all(checks.values()),
        "http_median_ms": {
            arm: median(row[arm]["http_ms"] for row in rows)
            for arm in ("reference_http", "candidate_http")
        },
        "verifier": {
            "calls": len(calls),
            "invalid_verdicts": sum(call["status"] == "invalid" for call in calls),
            "median_ms": median(call["duration_ms"] for call in calls) if calls else None,
            "usage": {
                key: sum(call["usage"].get(key) or 0 for call in calls)
                for key in ("input_tokens", "output_tokens", "cached_input_tokens")
            },
        },
    }


def development_questions(path):
    consumed = STUDY.parent / "v13-scope"
    known = {
        row["id"]: row
        for cohort in consumed.glob("round-*/questions.jsonl")
        for row in rows_at(cohort)
    }
    questions = rows_at(path)
    if not questions or any(known.get(q["id"]) != q for q in questions):
        raise ValueError("development requests must be unchanged questions from v1.3 rounds")
    return questions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--freeze-commit")
    modes.add_argument("--development", type=Path, help="previously consumed questions only")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = protocol()
    corpus = load_corpus()
    if spec["corpus_digest"] != corpus.digest:
        raise ValueError("trial corpus differs from the preregistered corpus")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".cache"):
        raise ValueError("raw results must stay under the ignored .cache directory")
    if args.freeze_commit:
        questions = frozen_questions(args.freeze_commit, spec, corpus)
        receipt = STUDY / "run.json"
    else:
        questions = development_questions(args.development)
        receipt = output / "run.json"
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "started_at": datetime.now(UTC).isoformat(),
        "freeze_commit": args.freeze_commit,
        "cohort_size": len(questions),
        "status": "started",
    }
    if args.freeze_commit:
        record["manifest_sha256"] = sha(STUDY / "manifest.json")
    with receipt.open("x") as file:
        json.dump(record, file, indent=2)
        file.write("\n")
    report = {**record, "rows": [], "errors": []}
    try:
        score_trial(questions, spec, corpus, output, report)
        summary = summarize(report, spec)
        report["summary"] = summary
        record.update(status="completed", passed=summary["passed"])
        print(json.dumps(summary, indent=2, sort_keys=True))
    except BaseException as error:
        record.update(status="failed", error=type(error).__name__)
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        report.update(record)
        write(output / "report.json", report)
        record.update(records=len(report["rows"]), report_sha256=sha(output / "report.json"))
        write(receipt, record)


if __name__ == "__main__":
    main()
