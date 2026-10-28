"""Resume checks structure/projection; source-intent and reserve issues remain auditable."""

from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from ledgerops.db.maintenance import (
    MaintenanceError,
    apply_rebuild,
    freeze_workspace,
    resume_workspace,
)
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration
ACTOR = "80000000-0000-4000-8000-000000000002"


def _state(db):
    tables = {
        "workspace": "id",
        "account_balance": "workspace_id,account_id",
        "operation": "workspace_id,operation_id",
        "request_result": "workspace_id,client_id,route_family,key",
        "journal": "workspace_id,journal_id",
        "journal_entry": "workspace_id,journal_id,line_no",
        "maintenance_freeze": "workspace_id,freeze_id",
        "projection_repair": "workspace_id,repair_id",
        "projection_repair_row": "workspace_id,repair_id,account_id",
    }
    with db.connect("reader") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
            for table, order in tables.items()
        }


def _freeze_and_refuse_repair(db, golden, expected_code):
    workspace = golden["workspace"]["id"]
    identifier = str(uuid4())
    with db.connect("maintenance") as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ledger_maintenance",)
        freeze_workspace(conn, workspace, identifier, ACTOR, "inspect existing issue")
        before = _state(db)
        with pytest.raises(MaintenanceError) as caught:
            apply_rebuild(conn, workspace, identifier, str(uuid4()), ACTOR, "repair", str(uuid4()))
        assert caught.value.code == expected_code
        assert _state(db) == before
    return identifier


def _resume_with_retained_warning(db, golden, freeze_id, issue_code):
    workspace = golden["workspace"]["id"]
    before = _state(db)
    with db.connect("maintenance") as conn:
        resumed = resume_workspace(conn, workspace, freeze_id, ACTOR, "resume after inspection")
        report = resumed["resume_report"]
        assert report["structure_ok"] and report["projection_ok"]
        assert issue_code in {issue["code"] for issue in report["issues"]}
        assert resumed["resumed_at"] is not None
        # Exact replay returns the report actually committed at resume, without
        # silently replacing it with a newly generated report of later state.
        assert (
            resume_workspace(conn, workspace, freeze_id, ACTOR, "resume after inspection")
            == resumed
        )
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "UPDATE ledger.maintenance_freeze SET resume_report=%s "
                "WHERE workspace_id=%s AND freeze_id=%s",
                (Jsonb({"issues": []}), workspace, freeze_id),
            )
    with db.connect("reader") as conn:
        assert conn.execute(
            "SELECT mode,current_freeze_id FROM ledger.workspace WHERE id=%s", (workspace,)
        ).fetchone() == ("NORMAL", None)
        assert conn.execute(
            "SELECT resume_report FROM ledger.maintenance_freeze "
            "WHERE workspace_id=%s AND freeze_id=%s",
            (workspace, freeze_id),
        ).fetchone() == (report,)
    after = _state(db)
    for table in before.keys() - {"workspace", "maintenance_freeze"}:
        assert after[table] == before[table]
    return report


def test_nonzero_reservation_blocks_repair_but_not_resume_and_is_retained(seeded_db, golden):
    customer = golden["purchase_request"]["customer_account_id"]
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=10 WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], customer),
        )
    freeze_id = _freeze_and_refuse_repair(seeded_db, golden, "RESERVED_UNSUPPORTED")
    report = _resume_with_retained_warning(seeded_db, golden, freeze_id, "UNSUPPORTED_RESERVATION")
    assert report["request_intent_ok"] and not report["reservation_supported"]
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked,reserved FROM ledger.account_balance "
            "WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], customer),
        ).fetchone() == (100, 10)
        current = verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))
    assert current.structure_ok and current.projection_ok
    assert not current.reservation_supported
    # A later legitimate-looking reserve release changes the live report. Resume
    # replay still exposes the original warning instead of rewriting history.
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=0 WHERE workspace_id=%s AND account_id=%s",
            (golden["workspace"]["id"], customer),
        )
    with seeded_db.connect("maintenance") as conn:
        replay = resume_workspace(
            conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume after inspection"
        )
        assert replay["resume_report"] == report
        assert not replay["resume_report"]["reservation_supported"]


def test_wrong_owner_blocks_repair_but_not_resume_and_is_retained(seeded_db, golden):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    request = golden["purchase_request"]
    wrong_merchant = golden["accounts"][3]["id"]
    # Privileged test-only corruption demonstrates the SQL-valid defect: entries
    # and projection agree on M2 while the retained canonical request names M1.
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.journal_entry SET account_id=%s "
            "WHERE workspace_id=%s AND account_id=%s AND side='C'",
            (wrong_merchant, request["workspace_id"], request["merchant_account_id"]),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.account_balance SET credits=0 WHERE workspace_id=%s AND account_id=%s",
            (request["workspace_id"], request["merchant_account_id"]),
        )
        conn.execute(
            "UPDATE ledger.account_balance SET credits=80 WHERE workspace_id=%s AND account_id=%s",
            (request["workspace_id"], wrong_merchant),
        )
    freeze_id = _freeze_and_refuse_repair(seeded_db, golden, "INVALID_SOURCE")
    report = _resume_with_retained_warning(seeded_db, golden, freeze_id, "WRONG_OWNER")
    assert not report["request_intent_ok"] and report["reservation_supported"]
    with seeded_db.connect("reader") as conn:
        current = verify_snapshot(capture_snapshot(conn, request["workspace_id"]))
        assert conn.execute(
            "SELECT booked FROM ledger.account_balance ORDER BY account_id"
        ).fetchall() == [(100,), (20,), (0,), (80,)]
    assert current.structure_ok and current.projection_ok and not current.request_intent_ok
    assert "WRONG_OWNER" in {issue.code for issue in current.issues}
