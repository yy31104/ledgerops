"""Apply immutable, checksummed SQL migrations as the non-login schema owner."""

import hashlib
import os
from importlib.resources import files

import psycopg
from psycopg import sql


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Use an autocommit migration connection allowed to SET ROLE ledger_migrator.

    The caller provisions roles and an isolated database first. This function never
    drops a database, creates login credentials, or changes existing migration bytes.
    """
    if not conn.autocommit:
        raise ValueError("migration connection must use autocommit with an explicit transaction")
    applied = []
    with conn.transaction():
        conn.execute("SET LOCAL ROLE ledger_migrator")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ledgerops:migrations', 0))")
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO ledger_app").format(sql.Identifier(database))
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("CREATE SCHEMA IF NOT EXISTS ledger AUTHORIZATION ledger_migrator")
        conn.execute("REVOKE ALL ON SCHEMA ledger FROM PUBLIC")
        conn.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ledger.schema_migration (
                version text PRIMARY KEY, sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        for path in sorted(files("ledgerops.db").joinpath("sql").iterdir(), key=lambda p: p.name):
            if not path.name.endswith(".sql"):
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            existing = conn.execute(
                "SELECT sha256 FROM ledger.schema_migration WHERE version = %s", (path.name,)
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError(f"migration checksum mismatch: {path.name}")
                continue
            conn.execute(content.decode("utf-8"))
            conn.execute(
                "INSERT INTO ledger.schema_migration (version, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    with psycopg.connect(os.environ["LEDGEROPS_MIGRATION_DSN"], autocommit=True) as connection:
        print("Applied:", apply_migrations(connection))
