"""Initialize the dedicated assistant DB, or serve the local product."""

import argparse
import os
import sys

import psycopg
from psycopg import sql

from ledgerops_assistant.context import ContextRetriever
from ledgerops_assistant.corpus import load_corpus
from ledgerops_assistant.drafts import setup as setup_drafts
from ledgerops_assistant.embeddings import BgeSmallEmbedder, HashingEmbedder
from ledgerops_assistant.postgres import PostgresRetriever, corpus_key, index_corpus, setup
from ledgerops_assistant.retrieval import HybridRetriever
from ledgerops_assistant.settings import Settings


def encoder(settings):
    if settings.embedder == "bge":
        return BgeSmallEmbedder()
    if settings.embedder == "hashing":
        return HashingEmbedder(384)
    raise ValueError("unknown embedder")


def retriever_for(settings):
    if not settings.database_url:
        raise ValueError("set LEDGEROPS_ASSISTANT_DATABASE_URL")
    corpus, embedder = load_corpus(), encoder(settings)
    key = corpus_key(corpus, embedder)
    with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
        found = conn.execute(
            "SELECT revision FROM assistant_rag.corpora WHERE corpus_id=%s", (key,)
        ).fetchone()
    if not found:
        raise ValueError("initialize this corpus and embedding revision before serving")
    retriever = HybridRetriever(
        PostgresRetriever(settings.database_url, key, embedder, method="fts"),
        PostgresRetriever(settings.database_url, key, embedder, method="vector"),
    )
    retriever.revision = found[0]
    return ContextRetriever(retriever, corpus)


def initialize(settings):
    # Refuse accidental use of the financial database before making any changes.
    with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
        if conn.execute("SELECT to_regnamespace('ledger')").fetchone()[0] is not None:
            raise ValueError("refusing to initialize an assistant inside a ledger database")
    setup(settings.database_url)
    setup_drafts(settings.database_url)
    print("assistant_init: schema ready; loading pinned local embeddings")
    index_corpus(settings.database_url, load_corpus(), encoder(settings))
    print("assistant_init: corpus indexed")
    password = os.environ.get("ASSISTANT_RUNTIME_PASSWORD")
    if password:
        with psycopg.connect(settings.database_url) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='assistant_runtime'"
            ).fetchone()
            command = "ALTER ROLE" if exists else "CREATE ROLE"
            conn.execute(
                sql.SQL(
                    command + " assistant_runtime LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Literal(password))
            )
            conn.execute(
                "GRANT USAGE ON SCHEMA assistant_rag,assistant_workflow,public TO assistant_runtime"
            )
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA assistant_rag TO assistant_runtime")
            conn.execute(
                "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA "
                "assistant_workflow,public TO assistant_runtime"
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "serve"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    try:
        settings = Settings.environment()
        if args.command == "init":
            initialize(settings)
            print("Assistant schema and corpus initialized; runtime permissions granted.")
        else:
            import uvicorn

            from ledgerops_assistant.api import create_app

            app = create_app(settings)
            uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    except Exception:
        print(
            "Assistant startup failed; check the dedicated DB, configuration and model cache.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
