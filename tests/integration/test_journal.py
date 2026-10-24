import copy
from uuid import uuid4

import psycopg
import pytest

from tests.integration.helpers import (
    account_id,
    insert_entries,
    insert_header,
    insert_operation,
    seed_accounts,
    write_journal,
)

pytestmark = pytest.mark.integration


def historical_append(golden):
    journal = golden["journals"][0]
    return [
        {
            "workspace_id": journal["workspace_id"],
            "journal_id": journal["journal_id"],
            "line_no": 3,
            "account_id": account_id(golden, "M1"),
            "side": "D",
            "amount_minor": 5,
        },
        {
            "workspace_id": journal["workspace_id"],
            "journal_id": journal["journal_id"],
            "line_no": 4,
            "account_id": account_id(golden, "M2"),
            "side": "C",
            "amount_minor": 5,
        },
    ]


@pytest.mark.parametrize("defect", ["zero", "one", "unbalanced", "count", "total"])
def test_invalid_journal_fails_at_real_commit_and_rolls_back(db, golden, defect):
    seed_accounts(db, golden)
    journal = copy.deepcopy(golden["journals"][0])
    entries = copy.deepcopy(golden["entries"][:2])
    if defect == "zero":
        entries = []
    elif defect == "one":
        entries = entries[:1]
    elif defect == "unbalanced":
        entries[1]["amount_minor"] = 99
    elif defect == "count":
        journal["line_count"] = 3
    else:
        journal["total_minor"] = 101
    reached_commit = False
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable declaration"):
            with conn.transaction():
                insert_operation(
                    conn, journal["workspace_id"], journal["operation_id"], kind="OPENING"
                )
                insert_header(conn, journal)
                insert_entries(conn, entries)
                reached_commit = True
        assert reached_commit, "The intended failure must occur at transaction commit"
        for table in ("operation", "journal", "journal_entry"):
            assert conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone() == (0,)


def test_valid_header_and_entries_commit_and_early_constraint_check_succeeds(db, golden):
    seed_accounts(db, golden)
    with db.connect() as conn:
        with conn.transaction():
            write_journal(conn, golden, "OPENING")
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert conn.execute("SELECT count(*) FROM ledger.journal_entry").fetchone() == (2,)


def assert_historical_append_rejected(conn, golden):
    original = conn.execute(
        "SELECT journal_id, line_no, account_id, side, amount_minor "
        "FROM ledger.journal_entry ORDER BY journal_id, line_no"
    ).fetchall()
    reached_commit = False
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable declaration"):
        with conn.transaction():
            insert_entries(conn, historical_append(golden))
            reached_commit = True
    assert reached_commit
    assert (
        conn.execute(
            "SELECT journal_id, line_no, account_id, side, amount_minor "
            "FROM ledger.journal_entry ORDER BY journal_id, line_no"
        ).fetchall()
        == original
    )


def test_balanced_historical_append_is_rejected_at_commit(seeded_db, golden):
    with seeded_db.connect() as conn:
        assert_historical_append_rejected(conn, golden)


def test_immediate_then_deferred_does_not_disable_later_checks(db, golden):
    seed_accounts(db, golden)
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable declaration"):
            with conn.transaction():
                write_journal(conn, golden, "OPENING")
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                insert_entries(conn, historical_append(golden))
        assert conn.execute("SELECT count(*) FROM ledger.journal").fetchone() == (0,)


def test_early_immediate_check_refuses_incomplete_header(db, golden):
    seed_accounts(db, golden)
    journal = golden["journals"][0]
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable declaration"):
            with conn.transaction():
                insert_operation(
                    conn, journal["workspace_id"], journal["operation_id"], kind="OPENING"
                )
                insert_header(conn, journal)
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert conn.execute("SELECT count(*) FROM ledger.journal").fetchone() == (0,)


def test_unique_effect_unique_account_and_kind_scope_are_database_constraints(seeded_db, golden):
    opening = golden["journals"][0]
    with seeded_db.connect() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            insert_header(conn, {**opening, "journal_id": str(uuid4())})
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            duplicate_account = {**golden["entries"][0], "line_no": 3}
            insert_entries(conn, [duplicate_account])
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            operation = insert_operation(conn, opening["workspace_id"], kind="PURCHASE")
            insert_header(
                conn,
                {
                    **opening,
                    "journal_id": str(uuid4()),
                    "operation_id": operation,
                },
            )


def test_header_declaration_domain_bounds(seeded_db, golden):
    opening = golden["journals"][0]
    with seeded_db.connect() as conn:
        for fields in ({"line_count": 1}, {"line_count": 17}, {"total_minor": 0}):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                operation = insert_operation(conn, opening["workspace_id"], kind="OPENING")
                insert_header(
                    conn,
                    {
                        **opening,
                        "journal_id": str(uuid4()),
                        "operation_id": operation,
                        **fields,
                    },
                )


def test_savepoint_rollback_preserves_original_and_rechecks_new_append(seeded_db, golden):
    with seeded_db.connect() as conn, conn.transaction():
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                insert_entries(conn, historical_append(golden))
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert conn.execute("SELECT count(*) FROM ledger.journal_entry").fetchone() == (2,)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_declaration_check_mutant_is_detectable(seeded_db, golden):
    # Deliberate owner-only mutation in a disposable DB, proving the negative test's oracle.
    with seeded_db.connect(role="owner") as conn:
        conn.execute("ALTER TABLE ledger.journal DISABLE TRIGGER journal_declared_on_header")
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_declared_on_line")
    with seeded_db.connect() as conn:
        # Run the unchanged ordinary acceptance assertion against the damaged schema.
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            assert_historical_append_rejected(conn, golden)
        assert conn.execute("SELECT count(*) FROM ledger.journal_entry").fetchone() == (4,)
