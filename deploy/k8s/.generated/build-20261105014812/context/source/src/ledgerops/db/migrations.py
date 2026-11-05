"""Apply immutable, checksummed SQL migrations as the non-login schema owner."""

import argparse
import hashlib
import os
from importlib.resources import files

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

INDEX_ONLY_MIGRATION = "011_active_reservation_index.sql"


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
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO ledger_app, ledger_reader, ledger_maintenance, "
                "ledger_worker, ledger_reconciler, ledger_auth_reader, ledger_auth_admin, "
                "ledger_operator"
            ).format(sql.Identifier(database))
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


def apply_index_only_migration(conn: psycopg.Connection) -> list[str]:
    """Explicit 010→011 access-path upgrade; never grant or reopen CONNECT.

    A restored database stays physically closed. This entry point does not touch
    workspace mode, restored identities, credentials or financial records.
    """
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("index migration requires an idle autocommit connection")
    resources = sorted(
        (
            path
            for path in files("ledgerops.db").joinpath("sql").iterdir()
            if path.name.endswith(".sql")
        ),
        key=lambda path: path.name,
    )
    if len(resources) != 11 or resources[-1].name != INDEX_ONLY_MIGRATION:
        raise ValueError("index migration requires the exact known installed schema")
    contents = [(path.name, path.read_bytes()) for path in resources]
    installed = [(name, hashlib.sha256(raw).hexdigest()) for name, raw in contents]
    with conn.transaction():
        conn.execute("SET LOCAL ROLE ledger_migrator")
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='120s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ledgerops:migrations', 0))")
        actual = conn.execute(
            "SELECT version,sha256 FROM ledger.schema_migration ORDER BY version"
        ).fetchall()
        if actual == installed:
            return []
        if actual != installed[:-1]:
            raise ValueError("index migration requires exact predecessor names and checksums")
        conn.execute(contents[-1][1].decode("utf-8"))
        conn.execute(
            "INSERT INTO ledger.schema_migration(version,sha256) VALUES (%s,%s)",
            installed[-1],
        )
    return [INDEX_ONLY_MIGRATION]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="apply only known011 to exact010 without changing runtime CONNECT",
    )
    args = parser.parse_args()
    with psycopg.connect(os.environ["LEDGEROPS_MIGRATION_DSN"], autocommit=True) as connection:
        operation = apply_index_only_migration if args.index_only else apply_migrations
        print("Applied:", operation(connection))
