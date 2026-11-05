-- Assistant database only, using its migration owner. Never run on the ledger DB.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS assistant_rag;
CREATE TABLE IF NOT EXISTS assistant_rag.corpora (
    corpus_id text PRIMARY KEY,
    revision text NOT NULL,
    embedder text NOT NULL,
    content_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_rag.chunks (
    corpus_id text NOT NULL REFERENCES assistant_rag.corpora(corpus_id),
    chunk_id text NOT NULL,
    section_id text NOT NULL,
    path text NOT NULL,
    title text NOT NULL,
    heading text NOT NULL,
    body text NOT NULL,
    embedding vector(384) NOT NULL,
    search tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    PRIMARY KEY (corpus_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS chunks_search_idx ON assistant_rag.chunks USING gin(search);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON assistant_rag.chunks
    USING hnsw(embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
