"""Deferred reversal bindings exercised using actual app SQL, independently of admission."""

from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.pq import TransactionStatus

from tests.integration.helpers import insert_entries, insert_header, insert_operation
from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "defect",
    ["missing-attempt", "missing-effect", "rejected-effect", "rejected-journal", "wrong-total"],
)
def test_invalid_reversal_final_state_cannot_commit_even_via_direct_app_sql(
    seeded_db, golden, defect
):
    purchase = invoke(seeded_db, *purchase_input(golden))
    workspace = golden["workspace"]["id"]
    original = golden["purchase_request"]["operation_id"]
    reversal, journal = str(uuid4()), str(uuid4())
    tables = (
        "operation",
        "journal",
        "journal_entry",
        "reversal_attempt",
        "purchase_reversal_effect",
    )
    with seeded_db.connect("app") as conn:
        before = {
            table: conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone()
            for table in tables
        }
        reached_commit = False
        with pytest.raises(errors.CheckViolation):
            with conn.transaction():
                insert_operation(
                    conn,
                    workspace,
                    reversal,
                    kind="REVERSAL",
                    admission="REJECTED" if defect.startswith("rejected") else "ACCEPTED",
                )
                if defect != "missing-attempt":
                    conn.execute(
                        "INSERT INTO ledger.reversal_attempt "
                        "VALUES (%s,%s,%s,'REVERSAL','PURCHASE')",
                        (workspace, reversal, original),
                    )
                if defect not in {"missing-attempt", "missing-effect"}:
                    conn.execute(
                        "INSERT INTO ledger.purchase_reversal_effect VALUES (%s,%s,%s)",
                        (workspace, original, reversal),
                    )
                if defect != "missing-attempt" and defect != "rejected-effect":
                    amount = 79 if defect == "wrong-total" else 80
                    insert_header(
                        conn,
                        dict(
                            workspace_id=workspace,
                            journal_id=journal,
                            operation_id=reversal,
                            kind="REVERSAL",
                            line_count=2,
                            total_minor=amount,
                        ),
                    )
                    insert_entries(
                        conn,
                        [
                            dict(
                                workspace_id=workspace,
                                journal_id=journal,
                                line_no=index,
                                account_id=account,
                                side=side,
                                amount_minor=amount,
                            )
                            for index, account, side in [
                                (1, golden["purchase_request"]["merchant_account_id"], "D"),
                                (2, golden["purchase_request"]["customer_account_id"], "C"),
                            ]
                        ],
                    )
                reached_commit = True
        assert reached_commit, "unexpected SQL/setup error is not deferred binding evidence"
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert {
            table: conn.execute(f"SELECT count(*) FROM ledger.{table}").fetchone()
            for table in tables
        } == before
        assert conn.execute(
            "SELECT response_body FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s",
            (workspace, original),
        ).fetchone() == (purchase.body,)
