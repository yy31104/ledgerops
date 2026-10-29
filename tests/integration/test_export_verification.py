"""Actual reader credentials export committed service data and deliberate DB defects."""

import psycopg
import pytest

from ledgerops.export import capture_snapshot, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration


def _capture(db, golden):
    with db.connect(role="reader") as conn:
        assert conn.execute("SELECT session_user,current_user").fetchone() == (
            "ledger_reader",
            "ledger_reader",
        )
        return capture_snapshot(conn, golden["workspace"]["id"])


def _post_actual_purchase(db, golden):
    result = invoke(db, *purchase_input(golden))
    assert result.http_status == 201 and not result.replayed
    return result


def test_real_reader_exports_and_verifies_service_without_a_manual_manifest(
    seeded_db, golden, tmp_path
):
    result = _post_actual_purchase(seeded_db, golden)
    with seeded_db.connect(role="reader") as conn:
        output = export_workspace(
            conn,
            golden["workspace"]["id"],
            tmp_path,
            code_revision="42b6e08738f24eac1b0640899daf05878da388fb",
            dirty=True,
        )
    snapshot = load_export(output)
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.projection_ok and report.request_intent_ok
    assert report.healthy and report.rebuild_eligible
    assert snapshot["format_version"] == 7
    assert snapshot["reversal_attempts"] == snapshot["reversal_effects"] == []
    assert all(
        snapshot[key] == []
        for key in (
            "payouts",
            "reservations",
            "reservation_events",
            "dispatch_intents",
            "transport_attempts",
        )
    )
    assert report.intent_scope == ("PURCHASE", "REVERSAL", "PAYOUT")
    assert report.unverified_intent_kinds == (
        "OPENING",
        "PAYOUT_EXTERNAL_EFFECT",
        "PAYOUT_PROVIDER_AUTHENTICITY",
    )
    assert report.issues == ()
    assert len(snapshot["accounts"]) == len(snapshot["balances"]) == 4
    assert len(snapshot["operations"]) == len(snapshot["journals"]) == 2
    assert len(snapshot["entries"]) == 4 and len(snapshot["requests"]) == 1
    assert [row["booked"] for row in snapshot["balances"]] == ["100", "20", "80", "0"]
    retained = next(r for r in snapshot["operations"] if r["kind"] == "PURCHASE")
    assert retained["response_body"] == result.body
    assert snapshot["requests"][0]["response_body"] == result.body
    # The OPENING payload is preserved, not replaced with an invented request.
    opening = next(r for r in snapshot["operations"] if r["kind"] == "OPENING")
    assert bytes.fromhex(opening["payload_hex"]) == b"explicit fixture operation"


def test_committed_projection_defect_is_found_without_failing_stored_request_check(
    seeded_db, golden
):
    _post_actual_purchase(seeded_db, golden)
    request = golden["purchase_request"]
    # Explicit trusted setup simulates a service defect which SQL permits: the
    # journal is correct, but the customer projection did not reflect its debit.
    with seeded_db.connect(role="app") as conn:
        assert conn.execute(
            "UPDATE ledger.account_balance SET debits=0 WHERE workspace_id=%s AND account_id=%s "
            "RETURNING booked",
            (request["workspace_id"], request["customer_account_id"]),
        ).fetchone() == (100,)
    snapshot = _capture(seeded_db, golden)
    customer = next(
        r for r in snapshot["balances"] if r["account_id"] == request["customer_account_id"]
    )
    assert customer["booked"] == "100"  # independent reader sees the actual committed defect
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.request_intent_ok
    assert not report.projection_ok and not report.healthy
    assert report.rebuild_eligible
    drift = [issue for issue in report.issues if issue.code == "PROJECTION_DRIFT"]
    assert {(i.object_id, i.field, i.expected, i.actual) for i in drift} == {
        (request["customer_account_id"], "debits", "80", "0"),
        (request["customer_account_id"], "booked", "20", "100"),
    }


def test_committed_wrong_merchant_matches_fold_but_saved_request_blocks_rebuild(seeded_db, golden):
    _post_actual_purchase(seeded_db, golden)
    request = golden["purchase_request"]
    merchant = request["merchant_account_id"]
    wrong_merchant = golden["accounts"][3]["id"]
    with seeded_db.connect(role="owner") as conn, conn.transaction():
        # Database owner corruption is a declared trust boundary. Only this
        # mutation guard is paused; financial CHECK/FK guards stay in force.
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
        assert conn.execute(
            "UPDATE ledger.journal_entry SET account_id=%s "
            "WHERE workspace_id=%s AND account_id=%s AND side='C' RETURNING amount_minor",
            (wrong_merchant, request["workspace_id"], merchant),
        ).fetchone() == (80,)
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")
        for account, credits in [(merchant, 0), (wrong_merchant, 80)]:
            conn.execute(
                "UPDATE ledger.account_balance SET credits=%s "
                "WHERE workspace_id=%s AND account_id=%s",
                (credits, request["workspace_id"], account),
            )
        assert conn.execute(
            "SELECT tgenabled FROM pg_trigger WHERE tgname='journal_entry_immutable'"
        ).fetchone() == ("O",)
    snapshot = _capture(seeded_db, golden)
    assert [r["booked"] for r in snapshot["balances"]] == ["100", "20", "0", "80"]
    original = next(r for r in snapshot["operations"] if r["kind"] == "PURCHASE")
    assert merchant.encode() in bytes.fromhex(original["payload_hex"])
    report = verify_snapshot(snapshot)
    assert report.structure_ok and report.projection_ok
    assert not report.request_intent_ok and not report.rebuild_eligible
    wrong = [i for i in report.issues if i.code == "WRONG_OWNER"]
    assert len(wrong) == 1 and wrong[0].object_id == request["operation_id"]
    assert (merchant, "C", "80") in wrong[0].expected
    assert (wrong_merchant, "C", "80") in wrong[0].actual


def test_missing_idle_projection_is_reported_in_real_reader_snapshot(seeded_db, golden):
    _post_actual_purchase(seeded_db, golden)
    idle = golden["accounts"][3]["id"]
    with seeded_db.connect(role="owner") as conn:
        assert conn.execute(
            "DELETE FROM ledger.account_balance WHERE workspace_id=%s "
            "AND account_id=%s RETURNING booked",
            (golden["workspace"]["id"], idle),
        ).fetchone() == (0,)
    report = verify_snapshot(_capture(seeded_db, golden))
    assert report.structure_ok and report.request_intent_ok
    assert not report.projection_ok and report.rebuild_eligible
    assert [(i.code, i.object_id) for i in report.issues] == [("MISSING_PROJECTION", idle)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger.account_balance SET debits=debits",
        "UPDATE ledger.workspace SET mode='FROZEN'",
        "INSERT INTO ledger.operation SELECT * FROM ledger.operation",
        "DELETE FROM ledger.request_result",
        "TRUNCATE ledger.journal CASCADE",
        "CREATE TABLE ledger.reader_bypass (id integer)",
        "CREATE TABLE public.reader_bypass (id integer)",
        "CREATE TEMP TABLE reader_bypass (id integer)",
        "ALTER TABLE ledger.journal DISABLE TRIGGER ALL",
        "SET ROLE ledger_app",
        "SET ROLE ledger_migrator",
        "SET ROLE postgres",
    ],
)
def test_reader_cannot_write_or_escalate_even_outside_read_only_export(seeded_db, statement):
    # Actual login privileges, not merely SET TRANSACTION READ ONLY, protect this.
    with seeded_db.connect(role="reader") as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ledger_reader",)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        assert conn.execute("SELECT count(*) FROM ledger.account").fetchone() == (4,)
