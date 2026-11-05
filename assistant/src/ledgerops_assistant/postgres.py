"""Immutable document snapshots, PostgreSQL FTS and pgvector, in an assistant DB."""

import hashlib
import math
from importlib.resources import files

import numpy as np
import psycopg

from ledgerops_assistant.corpus import Chunk, Corpus
from ledgerops_assistant.embeddings import Embedder
from ledgerops_assistant.retrieval import Hit, tokenize

DIMENSIONS = 384


def _vector(values) -> str:
    values = np.asarray(values, dtype=np.float32)
    if values.shape != (DIMENSIONS,) or not np.isfinite(values).all():
        raise ValueError("expected a finite 384-dimensional embedding")
    if not math.isclose(float(np.linalg.norm(values)), 1, abs_tol=1e-4):
        raise ValueError("expected a normalized, nonzero embedding")
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def corpus_key(corpus: Corpus, embedder: Embedder) -> str:
    return hashlib.sha256(f"{corpus.digest}\0{embedder.name}".encode()).hexdigest()


def setup(dsn: str) -> None:
    sql = files("ledgerops_assistant").joinpath("sql/001_rag.sql").read_text()
    with psycopg.connect(dsn) as conn:
        conn.execute(sql)


def index_corpus(dsn: str, corpus: Corpus, embedder: Embedder) -> str:
    key = corpus_key(corpus, embedder)
    with psycopg.connect(dsn) as conn:
        if conn.execute(
            "SELECT 1 FROM assistant_rag.corpora WHERE corpus_id=%s", (key,)
        ).fetchone():
            return key
    # Inference finishes before opening the write transaction.
    vectors = embedder.embed_passages([c.text for c in corpus.chunks])
    if vectors.shape != (len(corpus.chunks), DIMENSIONS):
        raise ValueError("embedding model must return one 384-dimensional vector per chunk")
    rows = [
        (key, c.chunk_id, c.section_id, c.path, c.title, c.heading, c.text, _vector(v))
        for c, v in zip(corpus.chunks, vectors, strict=True)
    ]
    with psycopg.connect(dsn) as conn:
        inserted = conn.execute(
            "INSERT INTO assistant_rag.corpora VALUES (%s,%s,%s,%s,DEFAULT) "
            "ON CONFLICT DO NOTHING RETURNING corpus_id",
            (key, corpus.revision, embedder.name, corpus.digest),
        ).fetchone()
        if inserted:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO assistant_rag.chunks "
                    "(corpus_id,chunk_id,section_id,path,title,heading,body,embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                    rows,
                )
    return key


class PostgresRetriever:
    def __init__(self, dsn: str, corpus_id: str, embedder: Embedder, *, method: str):
        if method not in ("fts", "vector", "exact"):
            raise ValueError("method must be fts, vector or exact")
        self.dsn, self.corpus_id, self.embedder = dsn, corpus_id, embedder
        self.method = method
        self.name = {"fts": "postgres_fts", "vector": "pgvector_hnsw", "exact": "pgvector_exact"}[
            method
        ]

    def search(self, query: str, k: int = 10) -> list[Hit]:
        if not 1 <= k <= 100:
            raise ValueError("k must be between 1 and 100")
        terms = list(dict.fromkeys(tokenize(query)))[:128]
        if self.method == "fts" and not terms:
            return []
        encoded = None if self.method == "fts" else _vector(self.embedder.embed_query(query))
        fields = "chunk_id,section_id,path,title,heading,body"
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            if self.method == "fts":
                rows = conn.execute(
                    f"SELECT {fields},ts_rank_cd(search,q,32) AS score "
                    "FROM assistant_rag.chunks,websearch_to_tsquery('english',%s) q "
                    "WHERE corpus_id=%s AND search @@ q ORDER BY score DESC,chunk_id LIMIT %s",
                    (" OR ".join(terms), self.corpus_id, k),
                ).fetchall()
            else:
                if self.method == "exact":
                    conn.execute("SET LOCAL enable_indexscan = off")
                    conn.execute("SET LOCAL enable_bitmapscan = off")
                else:
                    conn.execute("SET LOCAL hnsw.ef_search = 100")
                    conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
                rows = conn.execute(
                    f"SELECT {fields},1-(embedding <=> %s::vector) AS score "
                    "FROM assistant_rag.chunks WHERE corpus_id=%s "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (encoded, self.corpus_id, encoded, k),
                ).fetchall()
        return [Hit(Chunk(*row[:6]), float(row[6])) for row in rows]
