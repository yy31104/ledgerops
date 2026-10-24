"""Inject PostgreSQL failures into the real admission path, without service hooks."""

import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import submit_purchase
from reference.journal_fold import assert_projection_matches, assert_purchase_matches
from tests.integration.helpers import fetch_ledger
from tests.integration.purchase_helpers import purchase_input

pytestmark = pytest.mark.integration

GUARD_NAME = "test_purchase_atomicity_guard"
STAGES = {
    "journal_entry": ("journal_entry", "INSERT", "NEW.line_no = 1", False),
    "merchant_projection": ("account_balance", "UPDATE", None, False),
    "operation_terminal": ("operation", "UPDATE", "NEW.admission = 'ACCEPTED'", False),
    "request_terminal": ("request_result", "UPDATE", "NEW.http_status = 201", False),
    "deferred_commit": ("request_result", "UPDATE", "NEW.http_status = 201", True),
}


def database_snapshot(db):
    """Raw complete business state, with deterministic row order and one snapshot."""
    tables = {
        "workspace": ("id",),
        "account": ("workspace_id", "id"),
        "account_balance": ("workspace_id", "account_id"),
        "operation": ("workspace_id", "operation_id"),
        "request_result": ("workspace_id", "client_id", "route_family", "key"),
        "journal": ("workspace_id", "journal_id"),
        "journal_entry": ("workspace_id", "journal_id", "line_no"),
    }
    with db.connect(role="app") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(
                sql.SQL("SELECT * FROM ledger.{} ORDER BY {}").format(
                    sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, order))
                )
            ).fetchall()
            for table, order in tables.items()
        }


def install_guard(db, golden, stage):
    """Trusted setup adds a temporary trigger to this test's disposable database."""
    request = golden["purchase_request"]
    table, event, condition, deferred = STAGES[stage]
    condition_sql = (
        sql.SQL(condition)
        if condition is not None
        else sql.SQL("NEW.account_id = {}::uuid").format(
            sql.Literal(request["merchant_account_id"])
        )
    )
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute("""
            CREATE FUNCTION ledger.test_purchase_atomicity_guard() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
            BEGIN
              IF TG_ARGV[0] <> 'journal_entry' THEN
                IF NOT EXISTS (
                  SELECT 1 FROM ledger.account_balance
                  WHERE account_id = TG_ARGV[1]::uuid AND debits = 80 AND booked = 20
                ) OR NOT EXISTS (
                  SELECT 1 FROM ledger.account_balance
                  WHERE account_id = TG_ARGV[2]::uuid AND credits = 80 AND booked = 80
                ) THEN
                  RAISE EXCEPTION 'guard reached before both intended projection updates';
                END IF;
              END IF;
              IF TG_ARGV[0] = 'deferred_commit' THEN
                IF NOT EXISTS (
                  SELECT 1 FROM ledger.operation
                  WHERE operation_id = TG_ARGV[3]::uuid AND admission = 'ACCEPTED'
                    AND http_status = 201 AND response_body IS NOT NULL
                ) OR NOT EXISTS (
                  SELECT 1 FROM ledger.request_result
                  WHERE operation_id = TG_ARGV[3]::uuid
                    AND http_status = 201 AND response_body IS NOT NULL
                ) THEN
                  RAISE EXCEPTION 'commit guard reached before both persisted terminal results';
                END IF;
              END IF;
              RAISE EXCEPTION USING ERRCODE = '23514',
                CONSTRAINT = 'test_purchase_atomicity_guard',
                MESSAGE = 'injected purchase failure at ' || TG_ARGV[0];
            END $$
        """)
        conn.execute(
            sql.SQL(
                "CREATE {constraint}TRIGGER {name} AFTER {event} ON ledger.{table} "
                "{deferred}FOR EACH ROW WHEN ({condition}) "
                "EXECUTE FUNCTION ledger.test_purchase_atomicity_guard({stage}, {customer}, "
                "{merchant}, {operation})"
            ).format(
                constraint=sql.SQL("CONSTRAINT " if deferred else ""),
                name=sql.Identifier(GUARD_NAME),
                event=sql.SQL(event),
                table=sql.Identifier(table),
                deferred=sql.SQL("DEFERRABLE INITIALLY DEFERRED " if deferred else ""),
                condition=condition_sql,
                stage=sql.Literal(stage),
                customer=sql.Literal(request["customer_account_id"]),
                merchant=sql.Literal(request["merchant_account_id"]),
                operation=sql.Literal(request["operation_id"]),
            )
        )


def remove_guard(db, stage):
    table = STAGES[stage][0]
    with db.connect(role="owner") as conn, conn.transaction():
        conn.execute(
            sql.SQL("DROP TRIGGER {} ON ledger.{}").format(
                sql.Identifier(GUARD_NAME), sql.Identifier(table)
            )
        )
        conn.execute("DROP FUNCTION ledger.test_purchase_atomicity_guard()")


@pytest.mark.parametrize("stage", STAGES)
def test_unexpected_sql_failure_rolls_back_every_effect_and_same_identity_can_retry(
    seeded_db, golden, stage
):
    principal, command, request_key = purchase_input(golden)
    before = database_snapshot(seeded_db)
    opening = fetch_ledger(seeded_db)
    install_guard(seeded_db, golden, stage)
    try:
        with seeded_db.connect(role="app") as conn:
            with pytest.raises(errors.CheckViolation) as caught:
                submit_purchase(conn, principal, command, request_key)
            assert caught.value.diag.constraint_name == GUARD_NAME
            assert caught.value.diag.message_primary == f"injected purchase failure at {stage}"
            assert conn.info.transaction_status == TransactionStatus.IDLE
        # No fake rejection, orphan request claim, partial journal or balance
        # increment survives; a separate connection observes the original state.
        assert database_snapshot(seeded_db) == before
    finally:
        remove_guard(seeded_db, stage)

    with seeded_db.connect(role="app") as conn:
        result = submit_purchase(conn, principal, command, request_key)
        assert result.http_status == 201 and result.replayed is False
        assert conn.info.transaction_status == TransactionStatus.IDLE
    after = fetch_ledger(seeded_db)
    assert len(after["journals"]) == 2 and len(after["entries"]) == 4
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    assert_purchase_matches(
        golden["purchase_request"],
        after["accounts"],
        after["journals"],
        after["entries"],
        opening["projections"],
        after["projections"],
    )
    committed = database_snapshot(seeded_db)
    with seeded_db.connect(role="app") as conn:
        replay = submit_purchase(conn, principal, command, request_key)
        assert replay.http_status == result.http_status and replay.body == result.body
        assert replay.replayed is True
    assert database_snapshot(seeded_db) == committed


def test_existing_transaction_rejected_before_any_write_without_aborting_caller(seeded_db, golden):
    principal, command, request_key = purchase_input(golden)
    before = database_snapshot(seeded_db)
    with seeded_db.connect(role="app") as conn, conn.transaction():
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        with pytest.raises(
            ValueError, match="purchase admission requires an idle autocommit connection"
        ):
            submit_purchase(conn, principal, command, request_key)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert database_snapshot(seeded_db) == before


def test_non_autocommit_connection_rejected_even_when_idle(seeded_db, golden):
    principal, command, request_key = purchase_input(golden)
    before = database_snapshot(seeded_db)
    with seeded_db.connect(role="app") as conn:
        conn.autocommit = False
        assert conn.info.transaction_status == TransactionStatus.IDLE
        with pytest.raises(
            ValueError, match="purchase admission requires an idle autocommit connection"
        ):
            submit_purchase(conn, principal, command, request_key)
        assert conn.info.transaction_status == TransactionStatus.IDLE
    assert database_snapshot(seeded_db) == before
