import psycopg
import pytest

from ledgerops.db.migrations import apply_migrations

pytestmark = pytest.mark.integration


def test_migration_is_idempotent_and_owned_by_nonlogin_role(db):
    with db.connect("admin") as conn:
        assert apply_migrations(conn) == []
        assert conn.execute("SELECT count(*) FROM ledger.schema_migration").fetchone() == (6,)
        owners = conn.execute("""
            SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class
            WHERE relnamespace = 'ledger'::regnamespace
        """).fetchall()
        assert owners == [("ledger_migrator",)]


def test_changed_migration_checksum_fails_closed(db):
    with db.connect("owner") as conn:
        conn.execute("UPDATE ledger.schema_migration SET sha256 = 'tampered'")
    with db.connect("admin") as conn, pytest.raises(ValueError, match="checksum mismatch"):
        apply_migrations(conn)


def test_runtime_cannot_run_migrations(db):
    with db.connect() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        apply_migrations(conn)
