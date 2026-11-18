"""Ask one question from the command line.

uv run --locked python -m ledgerops_assistant "Does a timeout release the hold?"
uv run --locked python -m ledgerops_assistant --provider openai_api --json "..."
"""

import argparse
import json
import os
import sys

from ledgerops_assistant.answer import ScopeGate, answer
from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.coverage import DocumentCoverage
from ledgerops_assistant.evaluation import CACHE_DIR
from ledgerops_assistant.policy import coverage_threshold, scope_threshold
from ledgerops_assistant.providers import get_provider
from ledgerops_assistant.retrieval import BM25Retriever, HybridRetriever, VectorRetriever

# Historical memory-only scope heuristic; not the PostgreSQL retrieval selection.
# Its original synthetic calibration set is archived; the threshold is unchanged.
DEFAULT_GATE = 0.61


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ask the LedgerOps operator assistant.")
    parser.add_argument("question")
    parser.add_argument("--provider", default="fake")
    parser.add_argument("--backend", choices=("postgres", "memory"), default="postgres")
    parser.add_argument("--setup-database", action="store_true")
    parser.add_argument("--method", choices=("bm25", "vector", "hybrid"), default="hybrid")
    parser.add_argument("--embedder", choices=("bge", "hashing"), default="bge")
    parser.add_argument("--gate", type=float)
    parser.add_argument(
        "--scope-verifier",
        choices=("subscription",),
        help="use the local ChatGPT subscription to check passage support",
    )
    parser.add_argument("--json", action="store_true", help="print the full answer record")
    args = parser.parse_args(argv)

    verifier = None
    if args.scope_verifier == "subscription":
        from ledgerops_assistant.providers.subscription import SubscriptionProvider
        from ledgerops_assistant.support_semantic import REQUEST_TIMEOUT, SubscriptionPassageSupport

        verifier = SubscriptionPassageSupport(SubscriptionProvider(timeout=REQUEST_TIMEOUT))

    corpus = load_corpus()
    if args.embedder == "bge":
        from ledgerops_assistant.embeddings import BgeSmallEmbedder

        embedder = BgeSmallEmbedder()
    else:
        from ledgerops_assistant.embeddings import HashingEmbedder

        embedder = HashingEmbedder()
    if args.backend == "postgres":
        from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup

        dsn = os.environ.get("LEDGEROPS_ASSISTANT_DATABASE_URL")
        if not dsn:
            raise ValueError("set LEDGEROPS_ASSISTANT_DATABASE_URL to the assistant database")
        if args.setup_database:
            setup(dsn)
        key = index_corpus(dsn, corpus, embedder)
        bm25 = PostgresRetriever(dsn, key, embedder, method="fts")
        vector = PostgresRetriever(dsn, key, embedder, method="vector")
    else:
        bm25 = BM25Retriever(corpus)
        vector = VectorRetriever(corpus, embedder, cache_dir=CACHE_DIR)
    retriever = {"bm25": bm25, "vector": vector, "hybrid": HybridRetriever(bm25, vector)}
    similarity = args.gate
    if similarity is None:
        similarity = scope_threshold() if verifier is not None else DEFAULT_GATE
    if verifier is None:
        gate = ScopeGate.for_corpus(vector, similarity, corpus)
        context = ContextRetriever(retriever[args.method], corpus)
        top_n = 5
    else:
        from ledgerops_assistant.support_semantic import (
            CONTEXT_SECTIONS,
            SUPPORT_THRESHOLD,
            SubscriptionContextRetriever,
        )

        gate = ScopeGate(
            vector,
            similarity,
            coverage=DocumentCoverage(corpus, coverage_threshold()),
            support=verifier,
            support_threshold=SUPPORT_THRESHOLD,
        )
        context = SubscriptionContextRetriever(retriever[args.method], corpus)
        top_n = CONTEXT_SECTIONS
    result = answer(
        args.question,
        context,
        get_provider(args.provider),
        gate=gate,
        top_n=top_n,
        revision=corpus.revision,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    print(result.text)
    if result.refused:
        print(f"\n(refused: {result.reason})")
    for citation in result.citations:
        print(f"  - {citation.section_id} @ {citation.revision[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
