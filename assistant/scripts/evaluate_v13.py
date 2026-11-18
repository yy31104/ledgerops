"""Calibrate passage support on development, or run one frozen comparison."""

import argparse
import json
import math
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from scripts.evaluate_v11 import Cached, sha, verify, write
from scripts.evaluate_v12 import wilson

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "assistant/eval"
STUDY = EVAL / "v13-scope"
PROTOCOL = STUDY / "protocol.json"
REFERENCE = "7e637477223983917257fbb96d9c85ffcace979e"
TARGET_COMMIT = "5e316fdb401399708ba54a1ebb5645aec60777ae"
DEV_FILES = [EVAL / v / "questions.jsonl" for v in ("v12-dev", "v12-holdout")]


def development_paths():
    paths = list(DEV_FILES)
    for trial in sorted(STUDY.glob("round-*")):
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
    scores = {row["score"] for row in rows if row["reference_admitted"]}
    # Crossing an observed score changes admission. Use the lowest float in
    # each interval to implement the registered more-permissive tie break.
    cutoffs = [0.0, *sorted(math.nextafter(score, math.inf) for score in scores)]
    curve = []
    for threshold in cutoffs:
        updated = [
            {**row, "admitted": row["reference_admitted"] and row["score"] >= threshold}
            for row in rows
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
    # Raw retrieval shares the exact released code and corpus. Prove the files
    # participating in ranking are byte-identical before sharing their ranks.
    checkout = ROOT / "assistant/src/ledgerops_assistant"
    for name in (
        "corpus.py",
        "embeddings.py",
        "postgres.py",
        "retrieval.py",
        "routing.py",
        "coverage.py",
        "context.py",
        "sql/001_rag.sql",
    ):
        path = checkout / name
        if path.read_bytes() != git("show", f"{REFERENCE}:{path.relative_to(ROOT)}"):
            raise ValueError(f"released retrieval/router changed: {name}")
    if PROTOCOL.read_bytes() != git("show", f"{TARGET_COMMIT}:{PROTOCOL.relative_to(ROOT)}"):
        raise ValueError("the registered targets changed")


def measure(questions, *, threshold=None, update=None):
    """Use released ranking, real local embeddings and the same passages for both gates."""
    from dataclasses import asdict

    from ledgerops_assistant import routing
    from ledgerops_assistant.answer import ScopeGate, answer
    from ledgerops_assistant.context import ContextRetriever, retrieve_passages
    from ledgerops_assistant.corpus import load_corpus
    from ledgerops_assistant.coverage import DocumentCoverage
    from ledgerops_assistant.embeddings import BgeSmallEmbedder
    from ledgerops_assistant.evaluation import score_ranking
    from ledgerops_assistant.policy import coverage_threshold, scope_threshold
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
    from ledgerops_assistant.providers.fake import FakeProvider
    from ledgerops_assistant.retrieval import HybridRetriever, rank_sections
    from ledgerops_assistant.support import PassageSupport
    from scripts.evaluate_local import temporary_database

    pinned_reference()
    corpus = load_corpus()
    protocol = json.loads(PROTOCOL.read_text())
    if corpus.digest != protocol["reference"]["corpus_digest"]:
        raise ValueError("source corpus differs from the registered corpus")
    if (scope_threshold(), coverage_threshold()) != (0.5512, 0.597):
        raise ValueError("released scope thresholds differ from the reference")
    if any(set(q["relevant_sections"]) - corpus.section_ids() for q in questions):
        raise ValueError("question labels contain a missing section")
    coverage = DocumentCoverage(corpus, coverage_threshold())
    support = PassageSupport(coverage)
    embedder = BgeSmallEmbedder()
    rows = []
    with temporary_database() as dsn:
        setup(dsn)
        key = index_corpus(dsn, corpus, embedder)
        vector = Cached(PostgresRetriever(dsn, key, embedder, method="vector"))
        fts = Cached(PostgresRetriever(dsn, key, embedder, method="fts"))
        hybrid = Cached(HybridRetriever(fts, vector, rrf_k=10))
        context = ContextRetriever(hybrid, corpus)
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
            baseline = answer(text, context, provider, gate=reference, revision=corpus.revision)
            admitted = bool(baseline.provider)
            pack = retrieve_passages(context, text, 5) if admitted else []
            proof = support.measure(text, pack)
            row = {
                "id": question["id"],
                "expected_route": question["expected_route"],
                "group": question["group"],
                "category": question["category"],
                "reference_admitted": admitted,
                "reference_reason": baseline.reason,
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


def calibration(output):
    paths = development_paths()
    rows = measure(development_rows(paths))
    chosen, curve = select_threshold(rows)
    report = {
        "development_hashes": {str(path.relative_to(ROOT)): sha(path) for path in paths},
        "chosen": chosen,
        "reference": rates(rows, "reference_admitted"),
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
        (folder / "questions.jsonl").relative_to(ROOT).as_posix(),
        (STUDY / "development.json").relative_to(ROOT).as_posix(),
    }
    inputs = set(manifest["inputs_sha256"])
    code = set(manifest["code_sha256"])
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


def run(trial, output, expected_commit):
    if not 1 <= trial <= 3:
        raise ValueError("round must be from 1 to 3")
    folder = STUDY / f"round-{trial}"
    manifest, head = verify_frozen_checkout(folder, expected_commit)
    config = json.loads(
        (ROOT / "assistant/src/ledgerops_assistant/retrieval_policy.json").read_text()
    )
    threshold = manifest["support_threshold"]
    if config["support_threshold"] != threshold or config["method"] != "hybrid":
        raise ValueError("release settings do not match the frozen candidate")
    questions = [json.loads(line) for line in (folder / "questions.jsonl").read_text().splitlines()]
    counts = Counter(q["expected_route"] for q in questions)
    product = sum(q["category"] == "product_gap" for q in questions)
    if (
        counts["docs"] < 60
        or counts["out_of_scope"] < 60
        or product * 2 < counts["out_of_scope"]
        or counts.total() != counts["docs"] + counts["out_of_scope"]
        or len({q["id"] for q in questions}) != counts.total()
        or any(q["split"] != "test" for q in questions)
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

        rows = measure(questions, threshold=threshold, update=checkpoint)
        recall = paired_recall([r for r in rows if r["expected_route"] == "docs"])
        target = json.loads(PROTOCOL.read_text())["targets"]
        reference, candidate = rates(rows, "reference_admitted"), rates(rows, "candidate_admitted")
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
                    for k in ("reference", "candidate", "recall", "targets_met", "passed")
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
