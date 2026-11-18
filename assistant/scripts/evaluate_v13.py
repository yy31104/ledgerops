"""Calibrate passage support on development, or run one frozen comparison."""

import argparse
import json
import math
import runpy
import subprocess
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from scripts.evaluate_v11 import Cached, sha, verify, write
from scripts.evaluate_v12 import wilson

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "assistant/eval"
STUDY = EVAL / "v13-scope"
PROTOCOL = STUDY / "protocol.json"
CONTINUATION = STUDY / "CONTINUATION.md"
QUESTION_ROUTING = STUDY / "QUESTION-ROUTING.md"
SUBSCRIPTION_EVIDENCE = STUDY / "SUBSCRIPTION-EVIDENCE.md"
SUBSCRIPTION_CONTEXT = STUDY / "SUBSCRIPTION-CONTEXT.md"
FIT_RECORDS = {
    "compact-veto": STUDY / "compact-veto-fit.json",
    "conditional-veto": STUDY / "conditional-veto-fit.json",
}
REFERENCE = "7e637477223983917257fbb96d9c85ffcace979e"
TARGET_COMMIT = "5e316fdb401399708ba54a1ebb5645aec60777ae"
CONTINUATION_COMMIT = "2943f57fbe61b2b3226e5a222c899fc5ee605db1"
QUESTION_ROUTING_COMMIT = "04c4b56131c8d6175ce4b3ab6c817af1815fbf9a"
SUBSCRIPTION_EVIDENCE_COMMIT = "7aa4021f436804b737eeeadaceb7194cf437230c"
SUBSCRIPTION_CONTEXT_COMMIT = "75f167b61608c52abe3f164a7a8c122fe50b8dc5"
ORIGINAL_ROUNDS = 3
QUESTION_ROUTING_ROUND = 5
SUBSCRIPTION_ROUND = 6
SUBSCRIPTION_MODE = "subscription-evidence"
SUBSCRIPTION_MODEL = "gpt-6.1-sol"
DEV_FILES = [EVAL / v / "questions.jsonl" for v in ("v12-dev", "v12-holdout")]


def development_paths():
    paths = list(DEV_FILES)
    for trial in sorted(STUDY.glob("round-*"), key=lambda folder: int(folder.name[6:])):
        record = trial / "run.json"
        summary = trial / "summary.json"
        if (
            record.exists()
            and summary.exists()
            and json.loads(record.read_text())["status"] == "completed"
            and not json.loads(summary.read_text())["passed"]
        ):
            paths.append(trial / "questions.jsonl")
    return paths


def development_rows(paths):
    by_text = {}
    for path in paths:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row["expected_route"] in {"docs", "out_of_scope"}:
                by_text.setdefault(row["question"].strip().casefold(), row)
    return list(by_text.values())


def rates(rows, key, *, details=True):
    results = {}
    for name, route, wrong in (
        ("out_of_scope_passed", "out_of_scope", True),
        ("documentation_refused", "docs", False),
    ):
        cohort = [row for row in rows if row["expected_route"] == route]
        misses = [row["id"] for row in cohort if row[key] == wrong]
        results[name] = {
            "count": len(misses),
            "n": len(cohort),
            "rate": len(misses) / len(cohort),
            "wilson_ci95": wilson(len(misses), len(cohort)),
            **({"ids": misses} if details else {}),
        }
    return results


def select_threshold(rows):
    eligible = [row.get("candidate_eligible", row["reference_admitted"]) for row in rows]
    scores = {row["score"] for admitted, row in zip(eligible, rows, strict=True) if admitted}
    # Crossing an observed score changes admission. Use the lowest float in
    # each interval to implement the registered more-permissive tie break.
    cutoffs = [0.0, *sorted(math.nextafter(score, math.inf) for score in scores)]
    curve = []
    for threshold in cutoffs:
        updated = [
            {**row, "admitted": admitted and row["score"] >= threshold}
            for admitted, row in zip(eligible, rows, strict=True)
        ]
        outcome = rates(updated, "admitted", details=False)
        curve.append({"threshold": threshold, **outcome})

    def key(point):
        refused = point["documentation_refused"]["rate"]
        passed = point["out_of_scope_passed"]["rate"]
        if refused <= 0.05:
            return (False, passed, refused, point["threshold"])
        return (True, refused, passed, point["threshold"])

    return min(curve, key=key), curve


def paired_recall(rows, *, seed=20261118, resamples=10_000):
    import numpy as np

    groups = defaultdict(list)
    for row in rows:
        groups[row["group"]].append(row)
    deltas = [
        [row["candidate_recall"] - row["reference_recall"] for row in groups[group]]
        for group in sorted(groups)
    ]
    sums = np.asarray([sum(values) for values in deltas])
    sizes = np.asarray([len(values) for values in deltas])
    draws = np.random.default_rng(seed).integers(0, len(groups), size=(resamples, len(groups)))
    means = sums[draws].sum(axis=1) / sizes[draws].sum(axis=1)
    return {
        "n": len(rows),
        "clusters": len(groups),
        "reference": sum(row["reference_recall"] for row in rows) / len(rows),
        "candidate": sum(row["candidate_recall"] for row in rows) / len(rows),
        "difference": float(sums.sum() / sizes.sum()),
        "section_cluster_ci95": np.percentile(means, (2.5, 97.5), method="linear").tolist(),
    }


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def pinned_reference():
    from ledgerops_assistant import routing

    # Raw retrieval shares the exact released code and corpus. Prove the files
    # participating in ranking are byte-identical before sharing their ranks.
    checkout = ROOT / "assistant/src/ledgerops_assistant"
    for name in (
        "corpus.py",
        "embeddings.py",
        "postgres.py",
        "retrieval.py",
        "coverage.py",
        "context.py",
        "sql/001_rag.sql",
    ):
        path = checkout / name
        if path.read_bytes() != git("show", f"{REFERENCE}:{path.relative_to(ROOT)}"):
            raise ValueError(f"released retrieval changed: {name}")
    if PROTOCOL.read_bytes() != git("show", f"{TARGET_COMMIT}:{PROTOCOL.relative_to(ROOT)}"):
        raise ValueError("the registered targets changed")
    # Load the immutable reference router separately from the corrected runtime.
    relative = (checkout / "routing.py").relative_to(ROOT)
    with TemporaryDirectory(prefix="ledgerops-v12-router-") as directory:
        source = Path(directory) / "routing.py"
        source.write_bytes(git("show", f"{REFERENCE}:{relative}"))
        reference = runpy.run_path(str(source))
    if reference["_EXTERNAL_CAPABILITY"] != routing._EXTERNAL_CAPABILITY:
        raise ValueError("released topic list changed")
    return reference["classify"]


def measure(questions, *, threshold=None, update=None, semantic=False):
    """Collect eligibility, raw ranks and the selected candidate evidence pack."""
    from dataclasses import asdict

    from ledgerops_assistant import routing
    from ledgerops_assistant.answer import ScopeGate, answer
    from ledgerops_assistant.context import ContextRetriever, retrieve_passages
    from ledgerops_assistant.corpus import load_corpus
    from ledgerops_assistant.coverage import DocumentCoverage
    from ledgerops_assistant.embeddings import BgeSmallEmbedder
    from ledgerops_assistant.evaluation import score_ranking
    from ledgerops_assistant.policy import coverage_threshold, scope_threshold, support_mode
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
    from ledgerops_assistant.providers.fake import FakeProvider
    from ledgerops_assistant.retrieval import HybridRetriever, rank_sections
    from ledgerops_assistant.support import PassageSupport, Support
    from ledgerops_assistant.support_semantic import CONTEXT_SECTIONS, SubscriptionContextRetriever
    from scripts.evaluate_local import temporary_database

    classify_reference = pinned_reference()
    corpus = load_corpus()
    protocol = json.loads(PROTOCOL.read_text())
    if corpus.digest != protocol["reference"]["corpus_digest"]:
        raise ValueError("source corpus differs from the registered corpus")
    if (scope_threshold(), coverage_threshold()) != (0.5512, 0.597):
        raise ValueError("released scope thresholds differ from the reference")
    if any(set(q["relevant_sections"]) - corpus.section_ids() for q in questions):
        raise ValueError("question labels contain a missing section")
    coverage = DocumentCoverage(corpus, coverage_threshold())
    support = None if semantic else PassageSupport(coverage, mode=support_mode(), corpus=corpus)
    embedder = BgeSmallEmbedder()
    rows = []
    with temporary_database() as dsn:
        setup(dsn)
        key = index_corpus(dsn, corpus, embedder)
        vector = Cached(PostgresRetriever(dsn, key, embedder, method="vector"))
        fts = Cached(PostgresRetriever(dsn, key, embedder, method="fts"))
        hybrid = Cached(HybridRetriever(fts, vector, rrf_k=10))
        context = ContextRetriever(hybrid, corpus)
        candidate_context = SubscriptionContextRetriever(hybrid, corpus) if semantic else context
        top_n = CONTEXT_SECTIONS if semantic else 5
        reference = ScopeGate(vector, scope_threshold(), coverage=coverage)
        candidate = (
            ScopeGate(
                vector,
                scope_threshold(),
                coverage=coverage,
                support=support,
                support_threshold=threshold,
            )
            if threshold is not None
            else None
        )
        provider = FakeProvider()
        for question in questions:
            text = question["question"]
            baseline = answer(
                text,
                context,
                provider,
                gate=reference,
                revision=corpus.revision,
                classify_route=classify_reference,
            )
            eligible = answer(
                text,
                candidate_context,
                provider,
                gate=reference,
                revision=corpus.revision,
                top_n=top_n,
            )
            admitted = bool(eligible.provider)
            pack = retrieve_passages(candidate_context, text, top_n) if admitted else []
            proof = support.measure(text, pack) if support is not None else Support()
            row = {
                "id": question["id"],
                "expected_route": question["expected_route"],
                "group": question["group"],
                "category": question["category"],
                "reference_admitted": bool(baseline.provider),
                "reference_reason": baseline.reason,
                "candidate_eligible": admitted,
                "before_support_reason": eligible.reason,
                "score": proof.score,
                "support": asdict(proof),
                "passage_ids": [passage.chunk_id for passage in pack],
            }
            if candidate is not None:
                result = answer(text, context, provider, gate=candidate, revision=corpus.revision)
                row["candidate_admitted"] = bool(result.provider)
                row["candidate_reason"] = result.reason
                expected = admitted and proof.score >= threshold
                if row["candidate_admitted"] != expected:
                    raise ValueError("candidate admission differs from the calibrated decision")
            if question["expected_route"] == routing.DOCS:
                ranked = rank_sections(hybrid.search(text, 30))
                recall = score_ranking(ranked, question["relevant_sections"])["recall@5"]
                row.update(reference_recall=recall, candidate_recall=recall, ranked_sections=ranked)
            rows.append(row)
            if update:
                update(rows)
    return rows


def semantic_decisions(questions, rows, update):
    """Use the same local verifier as the CLI. Keep at most three requests in flight."""
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import asdict
    from time import perf_counter

    from ledgerops_assistant.corpus import load_corpus
    from ledgerops_assistant.providers.fake import FakeProvider
    from ledgerops_assistant.providers.subscription import SubscriptionProvider
    from ledgerops_assistant.support_semantic import (
        REQUEST_TIMEOUT,
        SUPPORT_THRESHOLD,
        SubscriptionPassageSupport,
    )

    chunks = load_corpus().by_id()
    texts = {question["id"]: question["question"] for question in questions}
    verifier = SubscriptionPassageSupport(
        SubscriptionProvider(model=SUBSCRIPTION_MODEL, timeout=REQUEST_TIMEOUT)
    )
    fake = FakeProvider()

    def verify_row(row):
        passages = [chunks[key] for key in row["passage_ids"]]
        start = perf_counter()
        proof = verifier.measure(texts[row["id"]], passages)
        elapsed = (perf_counter() - start) * 1000
        if proof.usage and (
            proof.usage["billing"] != "chatgpt_plan" or proof.usage["model"] != SUBSCRIPTION_MODEL
        ):
            raise ValueError("verifier billing or model differs from the freeze")
        admitted = row["candidate_eligible"] and proof.valid and proof.score >= SUPPORT_THRESHOLD
        reason = row["before_support_reason"]
        if admitted:
            draft = fake.draft(texts[row["id"]], passages)
            reason = draft.refusal_reason if draft.refused else "none"
        elif row["candidate_eligible"]:
            reason = "not_in_docs"
        return {
            **row,
            "score": proof.score,
            "support": asdict(proof),
            "candidate_admitted": admitted,
            "candidate_reason": reason,
            "scope_elapsed_ms": round(elapsed, 2) if proof.usage else None,
        }

    results = []
    pending = deque()

    def collect():
        results.append(pending.popleft().result())
        update(results)

    with ThreadPoolExecutor(max_workers=3) as pool:
        for row in rows:
            if len(pending) == 3:
                collect()
            pending.append(pool.submit(verify_row, row))
        while pending:
            collect()
    return results


def calibration(output):
    from ledgerops_assistant.policy import support_mode

    paths = development_paths()
    rows = measure(development_rows(paths))
    chosen, curve = select_threshold(rows)
    report = {
        "support_mode": support_mode(),
        "development_hashes": {str(path.relative_to(ROOT)): sha(path) for path in paths},
        "chosen": chosen,
        "reference": rates(rows, "reference_admitted"),
        "before_support": rates(rows, "candidate_eligible"),
        "curve": curve,
        "rows": rows,
    }
    with output.open("x") as file:
        json.dump(report, file, indent=2, allow_nan=False)
        file.write("\n")
    print(json.dumps({key: report[key] for key in ("chosen", "reference")}, indent=2))


def frozen_code_paths():
    assistant = ROOT / "assistant"
    return {
        path.relative_to(ROOT).as_posix()
        for path in [
            *(assistant / "src/ledgerops_assistant").rglob("*.py"),
            *(assistant / "src/ledgerops_assistant").rglob("*.sql"),
            assistant / "src/ledgerops_assistant/retrieval_policy.json",
            assistant / "scripts/evaluate_v13.py",
            *(assistant / "scripts").glob("fit_scope_*.py"),
            assistant / "scripts/evaluate_v11.py",
            assistant / "scripts/evaluate_v12.py",
            assistant / "scripts/evaluate_local.py",
            assistant / "pyproject.toml",
            assistant / "uv.lock",
        ]
    }


def verify_frozen_checkout(folder, expected_commit):
    head = git("rev-parse", "HEAD").decode().strip()
    if head != expected_commit or git("status", "--porcelain"):
        raise ValueError("run from the exact pushed freeze commit with a clean tracked tree")
    manifest_path = (folder / "manifest.json").relative_to(ROOT)
    if git("show", f"{head}:{manifest_path}") != (ROOT / manifest_path).read_bytes():
        raise ValueError("manifest differs from the frozen commit")
    manifest = verify(folder)
    required = {
        PROTOCOL.relative_to(ROOT).as_posix(),
        (STUDY / "REVIEW-METHOD.md").relative_to(ROOT).as_posix(),
        (folder / "questions.jsonl").relative_to(ROOT).as_posix(),
        (folder / "development.json").relative_to(ROOT).as_posix(),
        (folder / "audit-summary.json").relative_to(ROOT).as_posix(),
    }
    inputs = set(manifest["inputs_sha256"])
    code = set(manifest["code_sha256"])
    if manifest["round"] > ORIGINAL_ROUNDS:
        required.add(CONTINUATION.relative_to(ROOT).as_posix())
    if manifest["round"] >= QUESTION_ROUTING_ROUND:
        required.add(QUESTION_ROUTING.relative_to(ROOT).as_posix())
    if manifest["round"] >= SUBSCRIPTION_ROUND:
        required.update(
            path.relative_to(ROOT).as_posix()
            for path in (SUBSCRIPTION_EVIDENCE, SUBSCRIPTION_CONTEXT)
        )
    if fit_record := FIT_RECORDS.get(manifest.get("support_mode")):
        required.add(fit_record.relative_to(ROOT).as_posix())
    if not required <= inputs or not frozen_code_paths() <= code:
        raise ValueError("manifest omits required study or executable inputs")
    for name in sorted(inputs | code):
        if git("show", f"{head}:{name}") != (ROOT / name).read_bytes():
            raise ValueError(f"frozen input differs from the commit: {name}")
    ref = git("rev-parse", "--symbolic-full-name", "@{upstream}").decode().strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix):
        raise ValueError("freeze branch must track origin")
    ref = "refs/heads/" + ref.removeprefix(prefix)
    pushed = git("ls-remote", "--exit-code", "origin", ref).decode().split()
    if pushed != [head, ref]:
        raise ValueError("origin does not point to the requested freeze commit")
    return manifest, head


def verify_schedule(trial):
    if trial < 1:
        raise ValueError("round must be positive")
    if trial <= ORIGINAL_ROUNDS:
        return
    if CONTINUATION.read_bytes() != git(
        "show", f"{CONTINUATION_COMMIT}:{CONTINUATION.relative_to(ROOT)}"
    ):
        raise ValueError("the registered continuation changed")
    if trial >= QUESTION_ROUTING_ROUND and QUESTION_ROUTING.read_bytes() != git(
        "show", f"{QUESTION_ROUTING_COMMIT}:{QUESTION_ROUTING.relative_to(ROOT)}"
    ):
        raise ValueError("the registered question-routing correction changed")
    if trial >= SUBSCRIPTION_ROUND:
        for path, commit in (
            (SUBSCRIPTION_EVIDENCE, SUBSCRIPTION_EVIDENCE_COMMIT),
            (SUBSCRIPTION_CONTEXT, SUBSCRIPTION_CONTEXT_COMMIT),
        ):
            if path.read_bytes() != git("show", f"{commit}:{path.relative_to(ROOT)}"):
                raise ValueError("the registered subscription verification changed")
    for previous in range(1, trial):
        summary = STUDY / f"round-{previous}" / "summary.json"
        if not summary.is_file() or json.loads(summary.read_text())["passed"]:
            raise ValueError("a continuation requires a reported miss for every earlier round")


def run(trial, output, expected_commit):
    verify_schedule(trial)
    folder = STUDY / f"round-{trial}"
    manifest, head = verify_frozen_checkout(folder, expected_commit)
    if manifest["round"] != trial:
        raise ValueError("manifest and requested round differ")
    config = json.loads(
        (ROOT / "assistant/src/ledgerops_assistant/retrieval_policy.json").read_text()
    )
    threshold = manifest["support_threshold"]
    mode = manifest.get("support_mode", "single-span")
    semantic = mode == SUBSCRIPTION_MODE
    development = json.loads((folder / "development.json").read_text())
    if (
        semantic != (trial >= SUBSCRIPTION_ROUND)
        or development["support_threshold"] != threshold
        or config["method"] != "hybrid"
        or development.get("support_mode", "single-span") != mode
        or (
            not semantic
            and (
                config["support_threshold"] != threshold
                or config.get("support_mode", "single-span") != mode
            )
        )
    ):
        raise ValueError("release settings do not match the frozen candidate")
    if semantic:
        from ledgerops_assistant.support_semantic import (
            CONTEXT_BYTES,
            CONTEXT_SECTIONS,
            REQUEST_TIMEOUT,
            SUPPORT_THRESHOLD,
        )

        subscription = {
            "model": SUBSCRIPTION_MODEL,
            "context_sections": CONTEXT_SECTIONS,
            "context_bytes": CONTEXT_BYTES,
            "request_timeout_seconds": REQUEST_TIMEOUT,
        }
        if (
            threshold != SUPPORT_THRESHOLD
            or manifest["subscription"] != subscription
            or development["subscription"] != subscription
        ):
            raise ValueError("subscription profile differs from the frozen candidate")
    fit_record = FIT_RECORDS.get(mode)
    if fit_record and (
        development["fit_report_sha256"] != sha(fit_record)
        or json.loads(fit_record.read_text())["calibration"]["threshold"] != threshold
    ):
        raise ValueError("development cutoff does not match the frozen fit record")
    questions = [json.loads(line) for line in (folder / "questions.jsonl").read_text().splitlines()]
    counts = Counter(q["expected_route"] for q in questions)
    product = sum(
        q["expected_route"] == "out_of_scope" and q["category"] == "product_gap" for q in questions
    )
    if (
        counts["docs"] < 60
        or counts["out_of_scope"] < 60
        or product * 2 < counts["out_of_scope"]
        or counts.total() != counts["docs"] + counts["out_of_scope"]
        or len({q["id"] for q in questions}) != counts.total()
        or any(q["split"] != "test" for q in questions)
        or any(
            q["expected_route"] == "docs" and q["group"] not in q["relevant_sections"]
            for q in questions
        )
    ):
        raise ValueError("frozen cohort does not meet the registered population")
    if output.exists():
        raise FileExistsError(output)
    record = {
        "status": "running",
        "round": trial,
        "freeze_commit": head,
        "verified_origin_head": head,
        "manifest_sha256": sha(folder / "manifest.json"),
        "started_at": datetime.now(UTC).isoformat(),
    }
    record_path = folder / "run.json"
    with record_path.open("x") as file:
        file.write(json.dumps(record, indent=2) + "\n")
    report = {
        **record,
        "support_threshold": threshold,
        "support_mode": mode,
        "counts": dict(counts),
        "product_gaps": product,
    }
    reserved = False
    try:
        with output.open("x") as file:
            file.write(json.dumps(report, indent=2) + "\n")
        reserved = True

        def checkpoint(rows):
            report["rows"] = rows
            write(output, report)

        if semantic:
            report["subscription"] = subscription
            packs = measure(questions, update=checkpoint, semantic=True)
            rows = semantic_decisions(questions, packs, checkpoint)
            timings = [
                row["scope_elapsed_ms"] for row in rows if row["scope_elapsed_ms"] is not None
            ]
            report["scope_usage"] = {
                "requests": len(timings),
                "invalid_verdicts": sum(not row["support"]["valid"] for row in rows),
                "median_request_ms": median(timings) if timings else None,
                **{
                    field: sum(row["support"]["usage"].get(field, 0) or 0 for row in rows)
                    for field in ("input_tokens", "output_tokens", "cached_input_tokens")
                },
            }
        else:
            rows = measure(questions, threshold=threshold, update=checkpoint)
        recall = paired_recall([r for r in rows if r["expected_route"] == "docs"])
        target = json.loads(PROTOCOL.read_text())["targets"]
        reference, candidate = rates(rows, "reference_admitted"), rates(rows, "candidate_admitted")
        before_support = rates(rows, "candidate_eligible")
        met = {
            "out_of_scope": candidate["out_of_scope_passed"]["rate"]
            <= target["out_of_scope_pass_rate_max"],
            "documentation": candidate["documentation_refused"]["rate"]
            <= target["documentation_false_refusal_rate_max"],
            "retrieval": recall["section_cluster_ci95"][0]
            >= target["paired_recall_at_5_ci_lower_min"],
        }
        report.update(
            status="completed",
            recall=recall,
            reference=reference,
            before_support=before_support,
            candidate=candidate,
            targets_met=met,
            passed=all(met.values()),
        )
        write(output, report)
        record.update(status="completed", result_sha256=sha(output))
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "reference",
                        "before_support",
                        "candidate",
                        "recall",
                        "targets_met",
                        "passed",
                    )
                },
                indent=2,
            )
        )
        return 0
    except BaseException as error:
        record.update(status="failed", error_type=type(error).__name__)
        if reserved:
            report.update(status="failed", error_type=type(error).__name__)
            write(output, report)
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        write(record_path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--round", type=int)
    parser.add_argument("--freeze-commit")
    args = parser.parse_args()
    if args.round is None:
        calibration(args.output)
        return 0
    return run(args.round, args.output, args.freeze_commit)


if __name__ == "__main__":
    raise SystemExit(main())
