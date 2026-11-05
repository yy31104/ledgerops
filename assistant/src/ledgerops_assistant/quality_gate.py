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
    for method in ("postgres_fts", "pgvector_hnsw", "hybrid"):
        old = baseline["retrieval"][method]["by_split"]["dev"]["recall@5"]
        new = report["retrieval"][method]["by_split"]["dev"]["recall@5"]
        if new < old:
            raise ValueError(f"Recall@5 regression for {method}: {new} < {old}")
