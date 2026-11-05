import json
import secrets
import subprocess
import time
from uuid import uuid4

import numpy as np
import psycopg
import pytest

from ledgerops_assistant.corpus import Corpus, chunk_markdown
from ledgerops_assistant.embeddings import HashingEmbedder
from ledgerops_assistant.postgres import PostgresRetriever, index_corpus, setup

# pgvector 0.8.7, PostgreSQL 18; multi-platform registry digest.
IMAGE = "pgvector/pgvector@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47"


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=90
    ).stdout.strip()


@pytest.fixture(scope="session")
def pg_dsn():
    """Only creates and removes its own container; never uses a developer database."""
    password = secrets.token_urlsafe(24)
    name = "ledgerops-assistant-test-" + uuid4().hex[:12]
    docker(
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::5432",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        IMAGE,
    )
    try:
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["5432/tcp"][0][
            "HostPort"
        ]
        dsn = f"host=127.0.0.1 port={port} user=postgres password={password} dbname=postgres"
        for _ in range(60):
            try:
                with psycopg.connect(dsn, connect_timeout=1):
                    break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        setup(dsn)
        yield dsn
    finally:
        docker("rm", "--force", name)


@pytest.mark.postgres
def test_real_fts_vectors_hnsw_and_snapshot_isolation(pg_dsn, tiny_corpus):
    embedder = HashingEmbedder(384)
    key = index_corpus(pg_dsn, tiny_corpus, embedder)
    assert index_corpus(pg_dsn, tiny_corpus, embedder) == key
    changed = Corpus(
        tiny_corpus.root,
        "v2",
        tuple(chunk_markdown("other.md", "# Other\n\nUnrelated receipt data.\n")),
        {},
    )
    assert index_corpus(pg_dsn, changed, embedder) != key
    for method in ("fts", "vector", "exact"):
        hits = PostgresRetriever(pg_dsn, key, embedder, method=method).search("timeout hold")
        assert hits[0].chunk.section_id == "docs/PAYOUTS.md#dispatch-and-recovery"
        assert all(h.chunk.path == "docs/PAYOUTS.md" for h in hits)
    with psycopg.connect(pg_dsn) as conn:
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='assistant_rag'"
        ).fetchall()
        assert any("hnsw" in r[0] and "vector_cosine_ops" in r[0] for r in indexes)
        assert (
            conn.execute(
                "SELECT count(*) FROM assistant_rag.chunks WHERE corpus_id=%s", (key,)
            ).fetchone()[0]
            == 3
        )
        conn.execute("SET LOCAL enable_seqscan=off")
        plan = conn.execute(
            "EXPLAIN SELECT chunk_id FROM assistant_rag.chunks "
            "ORDER BY embedding <=> %s::vector LIMIT 5",
            ("[" + ",".join(map(str, embedder.embed_query("timeout"))) + "]",),
        ).fetchall()
        assert any("chunks_embedding_idx" in row[0] for row in plan)


@pytest.mark.postgres
def test_runtime_reader_cannot_mutate_index(pg_dsn, tiny_corpus):
    from psycopg import sql

    key = index_corpus(pg_dsn, tiny_corpus, HashingEmbedder(384))
    role, password = "reader_" + uuid4().hex[:8], secrets.token_urlsafe(24)
    with psycopg.connect(pg_dsn) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA assistant_rag TO {}").format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA assistant_rag TO {}").format(
                sql.Identifier(role)
            )
        )
    info = psycopg.conninfo.conninfo_to_dict(pg_dsn)
    info.update(user=role, password=password)
    reader_dsn = psycopg.conninfo.make_conninfo(**info)
    assert PostgresRetriever(reader_dsn, key, HashingEmbedder(384), method="fts").search("timeout")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(reader_dsn) as conn:
        conn.execute("DELETE FROM assistant_rag.chunks")


def test_rejects_nonfinite_and_wrong_dimension_vectors():
    from ledgerops_assistant.postgres import _vector

    for vector in (np.zeros(384), np.ones(383), np.full(384, np.nan)):
        with pytest.raises(ValueError):
            _vector(vector)
