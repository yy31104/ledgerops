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
    old_context, new_context = baseline.get("context", {}), report.get("context", {})
    for name in ("policy", "retrieval_method"):
        if name in old_context and new_context.get(name) != old_context[name]:
            raise ValueError(f"context {name} changed; review a new baseline")
    old_coverage = old_context.get("stats", {}).get("mean_section_recall")
    if old_coverage is not None:
        new_coverage = new_context.get("stats", {}).get("mean_section_recall", -1)
        if new_coverage < old_coverage:
            raise ValueError(
                f"expanded context coverage regression: {new_coverage} < {old_coverage}"
            )

    if "scope_gate" in baseline:
        if report.get("scope_gate", {}).get("threshold") != baseline["scope_gate"]["threshold"]:
            raise ValueError("scope threshold changed; review a new baseline")
