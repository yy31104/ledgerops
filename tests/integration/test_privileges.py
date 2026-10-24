import psycopg
import pytest

from tests.integration.helpers import account_id, insert_request, post_fixture

pytestmark = pytest.mark.integration

TABLES = (
    "workspace",
    "account",
    "account_balance",
    "operation",
    "request_result",
    "journal",
    "journal_entry",
)


def test_runtime_positive_write_matrix_and_real_login(seeded_db, golden):
    purchase = post_fixture(seeded_db, golden, "PURCHASE")
    with seeded_db.connect(role="app") as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == (
            "ledger_app",
            "ledger_app",
        )
        insert_request(conn, purchase["workspace_id"], purchase["operation_id"])
        for table in TABLES:
            assert conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone()[0] > 0
        balances = dict(
            conn.execute("SELECT account_id::text, booked FROM ledger.account_balance").fetchall()
        )
        assert balances == {
            account_id(golden, "clearing"): 100,
            account_id(golden, "customer"): 20,
            account_id(golden, "M1"): 80,
            account_id(golden, "M2"): 0,
        }


def test_runtime_role_has_no_ownership_or_escalation_grants(seeded_db):
    with seeded_db.connect() as conn:
        assert conn.execute(
            """SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
               FROM pg_roles WHERE rolname = current_user"""
        ).fetchone() == (False, False, False, False, False)
        assert conn.execute(
            "SELECT pg_has_role(current_user, 'ledger_migrator', 'MEMBER')"
        ).fetchone() == (False,)
        assert conn.execute(
            """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'ledger' AND c.relowner = current_user::regrole"""
        ).fetchone() == (0,)
        assert conn.execute(
            """SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'ledger' AND
               (p.proowner = current_user::regrole OR p.prosecdef
                OR has_function_privilege(current_user, p.oid, 'EXECUTE'))"""
        ).fetchone() == (0,)
        for table in TABLES:
            for privilege in ("DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"):
                assert not conn.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (f"ledger.{table}", privilege),
                ).fetchone()[0]


def test_runtime_cannot_mutate_history_identity_or_generated_balance(seeded_db):
    denied = [
        "UPDATE ledger.journal SET total_minor = total_minor",
        "UPDATE ledger.journal_entry SET amount_minor = amount_minor",
        "UPDATE ledger.account SET enabled = false",
        "UPDATE ledger.workspace SET mode = 'FROZEN'",
        "UPDATE ledger.account_balance SET booked = DEFAULT",
        "UPDATE ledger.account_balance SET normal_side = normal_side",
        "UPDATE ledger.account_balance SET account_id = account_id",
        "UPDATE ledger.operation SET payload = payload",
        "UPDATE ledger.operation SET operation_id = operation_id",
        "UPDATE ledger.request_result SET key = key",
        "UPDATE ledger.request_result SET payload = payload",
        "INSERT INTO ledger.account_balance (workspace_id, account_id, normal_side) "
        "SELECT workspace_id, account_id, normal_side FROM ledger.account_balance",
        "INSERT INTO ledger.account SELECT * FROM ledger.account",
        "INSERT INTO ledger.workspace SELECT * FROM ledger.workspace",
    ]
    denied += [f"DELETE FROM ledger.{table}" for table in TABLES]
    denied += [f"TRUNCATE TABLE ledger.{table} CASCADE" for table in TABLES]
    with seeded_db.connect() as conn:
        for statement in denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="permission denied"):
                conn.execute(statement)


def test_runtime_cannot_disable_guards_or_create_objects(seeded_db):
    denied = [
        "CREATE TABLE ledger.bypass (id integer)",
        "CREATE TABLE public.bypass (id integer)",
        "CREATE TEMP TABLE bypass (id integer)",
        "CREATE SCHEMA bypass",
        "ALTER TABLE ledger.journal DISABLE TRIGGER ALL",
        "ALTER TABLE ledger.journal DISABLE TRIGGER USER",
        "ALTER TABLE ledger.journal_entry DROP CONSTRAINT journal_entry_pkey",
        "SET session_replication_role = replica",
        "SET ROLE ledger_migrator",
        "SET ROLE postgres",
        "SET SESSION AUTHORIZATION postgres",
        "ALTER ROLE ledger_app SUPERUSER",
        "GRANT ledger_migrator TO ledger_app",
    ]
    with seeded_db.connect() as conn:
        for statement in denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_owner_history_triggers_catch_accidental_updates_and_deletes(seeded_db):
    # The owner remains a trust boundary: it could explicitly disable these guards.
    with seeded_db.connect(role="owner") as conn:
        for statement in (
            "UPDATE ledger.journal SET line_count = line_count",
            "DELETE FROM ledger.journal_entry",
            "DELETE FROM ledger.operation",
            "DELETE FROM ledger.account",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="immutable"):
                conn.execute(statement)
