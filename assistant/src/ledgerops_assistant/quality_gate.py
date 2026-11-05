"""A quality gate cannot be satisfied using stand-in embeddings or changed labels."""


def check_regression(report: dict, baseline: dict) -> None:
    for record in (report, baseline):
        if not record["environment"]["quality_signal"]:
            raise ValueError("quality gates require real bge embeddings")
        if record["environment"]["backend"] != "postgres":
            raise ValueError("quality gates require PostgreSQL retrieval")
    if report["environment"]["embedder"] != baseline["environment"]["embedder"]:
        raise ValueError("embedding model changed; review a new baseline")
    if report["questions"]["sha256"] != baseline["questions"]["sha256"]:
        raise ValueError("labels changed; review a new baseline")
    if "fusion" in baseline and report.get("fusion") != baseline["fusion"]:
        raise ValueError("fusion changed; review a new baseline")
    if (
        "corpus" in baseline
        and report.get("corpus", {}).get("digest") != baseline["corpus"]["digest"]
    ):
        raise ValueError("corpus changed; review a new baseline")
    for method in ("postgres_fts", "pgvector_hnsw", "hybrid"):
        old = baseline["retrieval"][method]["by_split"]["dev"]
        new = report["retrieval"][method]["by_split"]["dev"]
        for metric in ("recall@5", "mrr@10", "ndcg@5", "context_recall@5"):
            if metric in old and new.get(metric, -1) < old[metric]:
                raise ValueError(
                    f"{metric} regression for {method}: {new.get(metric)} < {old[metric]}"
                )
