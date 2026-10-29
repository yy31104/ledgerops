"""Repair live reservations from immutable sources without discarding pending money."""

import pytest

from ledgerops.db.maintenance import MaintenanceError, resume_workspace
from ledgerops.export import capture_snapshot, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.payout_helpers import admit, fund_merchant, money, payout_state
from tests.integration.test_maintenance import ACTOR, _apply, _freeze, _preview

pytestmark = pytest.mark.integration


def _report(db, golden):
    with db.connect("reader") as conn:
        return verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"]))


@pytest.mark.parametrize("drift", ["booked", "reserved", "missing"])
def test_active_hold_rebuild_repairs_projection_and_preserves_hold_history(
    seeded_db, golden, tmp_path, drift
):
    fund_merchant(seeded_db, golden)
    assert admit(seeded_db, golden).http_status == 202
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect("owner") as conn:
        if drift == "booked":
            conn.execute(
                "UPDATE ledger.account_balance SET credits=90 WHERE account_id=%s", (merchant,)
            )
        elif drift == "reserved":
            conn.execute(
                "UPDATE ledger.account_balance SET reserved=12 WHERE account_id=%s", (merchant,)
            )
        else:
            conn.execute("DELETE FROM ledger.account_balance WHERE account_id=%s", (merchant,))
    before = payout_state(seeded_db)
    report = _report(seeded_db, golden)
    assert report.structure_ok and report.request_intent_ok and report.reservation_supported
    assert not report.projection_ok and report.rebuild_eligible
    freeze = _freeze(seeded_db, golden)
    preview = _preview(seeded_db, golden, freeze, tmp_path)
    snapshot = load_export(preview["export_path"])
    assert snapshot["format_version"] == 5
    assert len(snapshot["reservation_events"]) == 1
    result = _apply(seeded_db, golden, freeze)
    assert (result["old_epoch"], result["new_epoch"]) == (1, 2)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    after = payout_state(seeded_db)
    for table in (
        "payout",
        "reservation",
        "reservation_event",
        "dispatch_intent",
        "transport_attempt",
        "journal",
        "journal_entry",
    ):
        assert after[table] == before[table]
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT fold_version FROM ledger.projection_repair").fetchone() == (2,)
        record = conn.execute(
            "SELECT before_reserved,after_reserved FROM ledger.projection_repair_row "
            "WHERE account_id=%s",
            (merchant,),
        ).fetchone()
        assert record == ({"booked": 62, "reserved": 12, "missing": None}[drift], 62)
    assert _report(seeded_db, golden).healthy
    with seeded_db.connect("maintenance") as conn:
        resumed = resume_workspace(
            conn, golden["workspace"]["id"], freeze, ACTOR, "checked active hold"
        )
    assert resumed["resume_report"]["projection_ok"]
    assert resumed["resume_report"]["reservation_supported"]
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_healthy_active_hold_can_freeze_and_resume_without_rebuild(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    before = payout_state(seeded_db)
    freeze = _freeze(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        resumed = resume_workspace(
            conn, golden["workspace"]["id"], freeze, ACTOR, "read-only inspection"
        )
    assert resumed["resume_report"]["healthy"]
    after = payout_state(seeded_db)
    before.pop("workspace")
    after.pop("workspace")
    assert after == before


@pytest.mark.parametrize("corruption", ["missing_event", "wrong_hold_amount", "wrong_hold_account"])
def test_invalid_authoritative_hold_source_blocks_rebuild_without_money_changes(
    seeded_db, golden, corruption
):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    # Explicit privileged corruption is test setup; maintenance cannot perform it.
    with seeded_db.connect("admin") as conn, conn.transaction():
        conn.execute("SET LOCAL session_replication_role=replica")
        if corruption == "missing_event":
            conn.execute("DELETE FROM ledger.reservation_event")
        elif corruption == "wrong_hold_amount":
            conn.execute("UPDATE ledger.reservation SET gross_minor=61")
        else:
            conn.execute(
                "UPDATE ledger.reservation SET account_id=%s", (golden["accounts"][3]["id"],)
            )
    freeze = _freeze(seeded_db, golden)
    before = payout_state(seeded_db)
    report = _report(seeded_db, golden)
    assert not report.rebuild_eligible
    with pytest.raises(MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze)
    assert caught.value.code == "INVALID_SOURCE"
    assert payout_state(seeded_db) == before


def test_failure_after_repair_reserved_update_rolls_back_projection_audit_and_epoch(
    seeded_db, golden
):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=12 WHERE account_id=%s",
            (golden["purchase_request"]["merchant_account_id"],),
        )
    freeze = _freeze(seeded_db, golden)
    before = payout_state(seeded_db)
    with seeded_db.connect("owner") as conn:
        conn.execute("""CREATE FUNCTION ledger.test_repair_hold_fault() RETURNS trigger
            LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='injected repair fault'; END $$""")
        conn.execute(
            "CREATE CONSTRAINT TRIGGER test_repair_hold_fault AFTER INSERT "
            "ON ledger.projection_repair_row DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION ledger.test_repair_hold_fault()"
        )
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        _apply(seeded_db, golden, freeze)
    assert payout_state(seeded_db) == before
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.projection_repair").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ledger.projection_repair_row").fetchone() == (0,)
        assert conn.execute("SELECT projection_epoch FROM ledger.workspace").fetchone() == (1,)
