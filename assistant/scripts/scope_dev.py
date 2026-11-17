"""Compare the v1.1 and current scope gates on development questions only.

Development here means every question set that has already been scored: the
current development file plus all earlier frozen and consumed sets. Rows are
deduplicated by question text; documentation rows are kept only when all of their
labelled sections exist in the current corpus. Generation is fake.

    uv run --locked python -m scripts.scope_dev --output .cache/scope-dev.json
"""

import argparse
import hashlib
import json
import subprocess
import types
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "assistant/eval"
CURRENT = EVAL / "v12-dev/questions.jsonl"
HISTORICAL = [
    CURRENT,
    EVAL / "v1-draft/questions.jsonl",
    EVAL / "refusal-v4/questions.jsonl",
    EVAL / "refusal-v5/questions.jsonl",
    EVAL / "v3-holdout/questions.jsonl",
    EVAL / "v2-holdout/questions.jsonl",
]
V11_COMMIT = "215f7db75fb150440350c1e82a2c4df1d4f395fb"  # v1.1.0 release merge
V11_ROUTING = "assistant/src/ledgerops_assistant/routing.py"
V11_SIMILARITY = 0.5512


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def v11_routing(commit=V11_COMMIT):
    """The released v1.1 routing module, loaded from Git for comparison only."""
    source = subprocess.check_output(["git", "show", f"{commit}:{V11_ROUTING}"], cwd=ROOT)
    module = types.ModuleType("ledgerops_v11_routing")
    exec(compile(source, f"v1.1:{V11_ROUTING}", "exec"), module.__dict__)  # noqa: S102
    return module


def pool(sections, files=HISTORICAL):
    seen, rows = set(), []
    for path in files:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["question"] in seen:
                continue
            seen.add(row["question"])
            if row["expected_route"] == "docs" and not set(row["relevant_sections"]) <= sections:
                continue
            rows.append({**row, "source": path.parent.name})
    return rows


def v11_decision(old, question, similarity):
    route = old.classify(question)
    if route == old.ACTION:
        return "action"
    if old.unsupported_topic(question):
        return "out_of_scope"
    if route == old.LIVE_STATE:
        return "live_state"
    return "docs" if similarity >= V11_SIMILARITY else "out_of_scope"


def rates(rows, key):
    docs = [r for r in rows if r["expected_route"] == "docs"]
    outside = [r for r in rows if r["expected_route"] == "out_of_scope"]
    passed = [r["id"] for r in outside if r[key] == "docs"]
    refused = [r["id"] for r in docs if r[key] != "docs"]
    return {
        "out_of_scope": len(outside),
        "out_of_scope_passed": len(passed),
        "out_of_scope_pass_rate": round(len(passed) / len(outside), 4) if outside else None,
        "documentation": len(docs),
        "documentation_refused": len(refused),
        "documentation_refusal_rate": round(len(refused) / len(docs), 4) if docs else None,
        "passed_ids": passed,
        "refused_ids": refused,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "assistant/.cache/scope-dev.json")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    from ledgerops_assistant import routing
    from ledgerops_assistant.answer import ScopeGate, answer
    from ledgerops_assistant.context import ContextRetriever
    from ledgerops_assistant.corpus import load_corpus
    from ledgerops_assistant.coverage import DocumentCoverage, calibrate
    from ledgerops_assistant.embeddings import BgeSmallEmbedder
    from ledgerops_assistant.policy import coverage_threshold, scope_threshold
    from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup
    from ledgerops_assistant.providers.fake import FakeProvider
    from ledgerops_assistant.retrieval import HybridRetriever
    from scripts.evaluate_local import temporary_database

    corpus = load_corpus()
    rows = pool(corpus.section_ids())
    current_ids = {json.loads(line)["id"] for line in CURRENT.read_text().splitlines()}
    coverage = DocumentCoverage(corpus, coverage_threshold())
    old = v11_routing()
    encoder = BgeSmallEmbedder()
    with temporary_database() as dsn:
        setup(dsn)
        key = index_corpus(dsn, corpus, encoder)
        vector = PostgresRetriever(dsn, key, encoder, method="vector")
        hybrid = HybridRetriever(PostgresRetriever(dsn, key, encoder, method="fts"), vector)
        gate = ScopeGate(vector, scope_threshold(), coverage=coverage)
        context = ContextRetriever(hybrid, corpus)
        for row in rows:
            hits = vector.search(row["question"], 1)
            row["similarity"] = round(hits[0].score, 4) if hits else 0.0
            measured = coverage.measure(row["question"])
            row["uncovered_share"] = measured.uncovered_share
            row["unknown_codes"] = list(measured.unknown_codes)
            row["v11"] = v11_decision(old, row["question"], row["similarity"])
            result = answer(row["question"], context, FakeProvider(), gate=gate)
            row["v12"] = "out_of_scope" if result.reason == "not_in_docs" else result.route
    # Calibrate on the questions that reach the coverage check in the current flow.
    documentation_routed = [
        (r["expected_route"] == "docs", coverage.measure(r["question"]))
        for r in rows
        if r["expected_route"] in ("docs", "out_of_scope")
        and routing.classify(r["question"]) == routing.DOCS
        and not routing.unsupported_topic(r["question"])
    ]
    current = [r for r in rows if r["id"] in current_ids and r["source"] == "v12-dev"]
    summary = {
        "corpus_digest": corpus.digest,
        "inputs_sha256": {p.relative_to(ROOT).as_posix(): sha(p) for p in HISTORICAL},
        "coverage_threshold": coverage.threshold,
        "similarity_threshold": scope_threshold(),
        "calibration": calibrate(documentation_routed),
        "pool": {
            "rows": len(rows),
            "by_route": dict(Counter(r["expected_route"] for r in rows)),
            "by_source": dict(Counter(r["source"] for r in rows)),
        },
        "current_development": {"v1.1": rates(current, "v11"), "v1.2": rates(current, "v12")},
        "all_development": {"v1.1": rates(rows, "v11"), "v1.2": rates(rows, "v12")},
        "route_accuracy": {
            name: round(sum(r[name] == r["expected_route"] for r in rows) / len(rows), 4)
            for name in ("v11", "v12")
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({**summary, "rows": rows}, indent=2) + "\n")
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("current_development", "all_development")}, indent=2))


if __name__ == "__main__":
    main()
