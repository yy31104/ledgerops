"""Actual maintenance authority, writer drain, repair atomicity and identity recovery.

Expected money/version values are literals or original input expectations. Privileged
corruption/funding below is explicit test setup, never a maintenance capability.
"""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from ledgerops.db import maintenance
from ledgerops.db.admission import RetrySameIdentity, submit_purchase
from ledgerops.db.maintenance import (
    MaintenanceError,
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
from ledgerops.domain import OperationId
from ledgerops.export import capture_snapshot, load_export
from ledgerops.verification import verify_snapshot
from reference.journal_fold import assert_projection_matches
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.helpers import fetch_ledger
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.purchase_helpers import fund_customer, invoke, purchase_input
from tests.integration.test_purchase_recovery import _wait_backend_gone

pytestmark = pytest.mark.integration
ACTOR = "80000000-0000-4000-8000-000000000002"
REVISION = "a067785d2b5addabf98a6121ea87b0f4b14ce4b9"


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
    with db.connect("admin") as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return {
            table: conn.execute(f"SELECT * FROM ledger.{table} ORDER BY {order}").fetchall()
            for table, order in tables.items()
        }


def _freeze(db, golden, freeze_id=None):
    identifier = freeze_id or str(uuid4())
    with db.connect("maintenance") as conn:
        assert conn.execute("SELECT session_user,current_user").fetchone() == (
            "ledger_maintenance",
            "ledger_maintenance",
        )
        result = freeze_workspace(
            conn, golden["workspace"]["id"], identifier, ACTOR, "repair preview"
        )
        assert str(result["freeze_id"]) == identifier
        assert conn.info.transaction_status == TransactionStatus.IDLE
    return identifier


def _preview(db, golden, freeze_id, output_root):
    with db.connect("maintenance") as conn:
        return preview_rebuild(
            conn,
            golden["workspace"]["id"],
            freeze_id,
            output_root,
            code_revision=REVISION,
        )


def _apply(db, golden, freeze_id, *, repair_id=None, preview_id=None):
    identifier = repair_id or str(uuid4())
    preview_id = preview_id or str(uuid4())
    with db.connect("maintenance") as conn:
        result = apply_rebuild(
            conn,
            golden["workspace"]["id"],
            freeze_id,
            identifier,
            ACTOR,
            "recompute from immutable entries",
            preview_id,
        )
        assert str(result["repair_id"]) == identifier
        assert conn.info.transaction_status == TransactionStatus.IDLE
    return result


def _drift(db, golden, *, missing_idle=False):
    request = golden["purchase_request"]
    assert invoke(db, *purchase_input(golden)).http_status == 201
    with db.connect("owner") as conn, conn.transaction():
        conn.execute(
            "UPDATE ledger.account_balance SET debits=0 WHERE workspace_id=%s AND account_id=%s",
            (request["workspace_id"], request["customer_account_id"]),
        )
        if missing_idle:
            conn.execute(
                "DELETE FROM ledger.account_balance WHERE workspace_id=%s AND account_id=%s",
                (request["workspace_id"], golden["accounts"][3]["id"]),
            )


def _assert_money(db, amounts):
    with db.connect("reader") as conn:
        actual = conn.execute(
            "SELECT booked FROM ledger.account_balance ORDER BY account_id"
        ).fetchall()
        assert actual == [(amount,) for amount in amounts]
    raw = fetch_ledger(db)
    assert_projection_matches(raw["accounts"], raw["entries"], raw["projections"])


def test_repair_restores_drift_and_missing_idle_row_with_complete_immutable_audit(
    seeded_db, golden, tmp_path
):
    _drift(seeded_db, golden, missing_idle=True)
    freeze_id = _freeze(seeded_db, golden)
    preview = _preview(seeded_db, golden, freeze_id, tmp_path)
    assert preview["report"]["rebuild_eligible"]
    assert not preview["report"]["projection_ok"]
    assert len(preview["changes"]) == 4
    idle = golden["accounts"][3]["id"]
    assert next(r for r in preview["changes"] if r["account_id"] == idle)["before"] is None
    preview_id = Path(preview["export_path"]).name
    result = _apply(seeded_db, golden, freeze_id, preview_id=preview_id)
    assert (result["old_epoch"], result["new_epoch"], result["account_count"]) == (1, 2, 4)
    assert str(result["preview_export_id"]) == preview_id
    _assert_money(seeded_db, [100, 20, 80, 0])
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT mode,projection_epoch,current_freeze_id FROM ledger.workspace"
        ).fetchone() == ("FROZEN", 2, UUID(freeze_id))
        rows = conn.execute(
            "SELECT account_id,before_debits,before_credits,before_booked,before_version,"
            "after_debits,after_credits,after_booked,after_version "
            "FROM ledger.projection_repair_row ORDER BY account_id"
        ).fetchall()
        assert [r[1:] for r in rows] == [
            (100, 0, 100, 1, 100, 0, 100, 2),
            (0, 100, 100, 2, 80, 100, 20, 3),
            (0, 80, 80, 1, 0, 80, 80, 2),
            (None, None, None, None, 0, 0, 0, 1),
        ]
        assert verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"])).healthy
    after_preview = _preview(seeded_db, golden, freeze_id, tmp_path)
    assert after_preview["report"]["healthy"]
    with seeded_db.connect("maintenance") as conn:
        resumed = resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "checked")
        assert resumed["resumed_at"] is not None
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT mode,current_freeze_id FROM ledger.workspace").fetchone() == (
            "NORMAL",
            None,
        )


def test_apply_recomputes_current_source_instead_of_using_preview_targets(
    seeded_db, golden, tmp_path
):
    _drift(seeded_db, golden)
    freeze_id = _freeze(seeded_db, golden)
    preview = _preview(seeded_db, golden, freeze_id, tmp_path)
    customer = golden["purchase_request"]["customer_account_id"]
    target = next(r for r in preview["changes"] if r["account_id"] == customer)
    assert target["after"]["booked"] == "20"
    # Deliberate raw-credential fixture setup bypasses the cooperating-writer
    # protocol. It proves application reads current sources rather than a file.
    fund_customer(seeded_db, golden, 10)
    _apply(seeded_db, golden, freeze_id, preview_id=Path(preview["export_path"]).name)
    _assert_money(seeded_db, [110, 30, 80, 0])
    old = load_export(Path(preview["export_path"]))
    assert next(r for r in old["balances"] if r["account_id"] == customer)["booked"] == "100"


def test_fresh_wrong_owner_source_blocks_apply_even_when_preview_was_eligible(
    seeded_db, golden, tmp_path
):
    _drift(seeded_db, golden)
    freeze_id = _freeze(seeded_db, golden)
    preview = _preview(seeded_db, golden, freeze_id, tmp_path)
    assert preview["report"]["rebuild_eligible"]
    request = golden["purchase_request"]
    wrong = golden["accounts"][3]["id"]
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.journal_entry SET account_id=%s WHERE workspace_id=%s "
            "AND account_id=%s AND side='C'",
            (wrong, request["workspace_id"], request["merchant_account_id"]),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.account_balance SET debits=80 WHERE account_id=%s",
            (request["customer_account_id"],),
        )
        conn.execute(
            "UPDATE ledger.account_balance SET credits=0 WHERE account_id=%s",
            (request["merchant_account_id"],),
        )
        conn.execute("UPDATE ledger.account_balance SET credits=80 WHERE account_id=%s", (wrong,))
    before = _state(seeded_db)
    with pytest.raises(MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze_id, preview_id=Path(preview["export_path"]).name)
    assert caught.value.code == "INVALID_SOURCE"
    assert any(issue["code"] == "WRONG_OWNER" for issue in caught.value.report["issues"])
    assert _state(seeded_db) == before


def test_nonzero_reserved_blocks_apply_without_erasing_reservations(seeded_db, golden):
    _drift(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=1 WHERE account_id=%s",
            (golden["purchase_request"]["merchant_account_id"],),
        )
    freeze_id = _freeze(seeded_db, golden)
    before = _state(seeded_db)
    with pytest.raises(MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze_id)
    assert caught.value.code == "RESERVED_UNSUPPORTED"
    assert _state(seeded_db) == before


def test_resume_rechecks_projection_and_repair_does_not_implicitly_resume(seeded_db, golden):
    _drift(seeded_db, golden)
    freeze_id = _freeze(seeded_db, golden)
    before = _state(seeded_db)
    with seeded_db.connect("maintenance") as conn:
        with pytest.raises(MaintenanceError) as caught:
            resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume")
        assert caught.value.code == "PROJECTION_DRIFT"
    assert _state(seeded_db) == before
    _apply(seeded_db, golden, freeze_id)
    with pytest.raises(RetrySameIdentity) as caught:
        invoke(seeded_db, *purchase_input(golden))
    assert caught.value.reason == "workspace_not_normal"
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET debits=0 WHERE account_id=%s",
            (golden["purchase_request"]["customer_account_id"],),
        )
    with seeded_db.connect("maintenance") as conn:
        with pytest.raises(MaintenanceError) as caught:
            resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume")
        assert caught.value.code == "PROJECTION_DRIFT"


def test_stable_maintenance_ids_replay_without_reapplying_or_reopening_a_freeze(seeded_db, golden):
    freeze_id = _freeze(seeded_db, golden)
    repair_id, preview_id = str(uuid4()), str(uuid4())
    first = _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    before = _state(seeded_db)
    assert _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id) == first
    assert _state(seeded_db) == before
    with seeded_db.connect("maintenance") as conn:
        result = resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume")
        assert (
            resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume") == result
        )
    second_freeze = _freeze(seeded_db, golden)
    before = _state(seeded_db)
    assert _freeze(seeded_db, golden, freeze_id) == freeze_id
    assert _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id) == first
    with seeded_db.connect("maintenance") as conn:
        assert (
            resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "resume") == result
        )
        with pytest.raises(MaintenanceError) as caught:
            apply_rebuild(
                conn, golden["workspace"]["id"], freeze_id, str(uuid4()), ACTOR, "new", preview_id
            )
        assert caught.value.code == "FREEZE_CONFLICT"
        with pytest.raises(MaintenanceError) as caught:
            freeze_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "changed")
        assert caught.value.code == "IDENTITY_CONFLICT"
        with pytest.raises(MaintenanceError) as caught:
            apply_rebuild(
                conn, golden["workspace"]["id"], freeze_id, repair_id, ACTOR, "changed", preview_id
            )
        assert caught.value.code == "IDENTITY_CONFLICT"
        with pytest.raises(MaintenanceError) as caught:
            resume_workspace(conn, golden["workspace"]["id"], freeze_id, ACTOR, "changed")
        assert caught.value.code == "IDENTITY_CONFLICT"
    assert _state(seeded_db) == before
    assert second_freeze != freeze_id


@pytest.mark.parametrize(
    "mode,code", [("NORMAL", "FROZEN_REQUIRED"), ("RESTORE", "RESTORE_UNSUPPORTED")]
)
def test_apply_requires_current_maintenance_freeze_not_restore_or_normal(
    seeded_db, golden, mode, code
):
    with seeded_db.connect("owner") as conn:
        conn.execute("UPDATE ledger.workspace SET mode=%s", (mode,))
    before = _state(seeded_db)
    with pytest.raises(MaintenanceError) as caught:
        _apply(seeded_db, golden, str(uuid4()))
    assert caught.value.code == code
    assert _state(seeded_db) == before


@pytest.mark.parametrize(
    "field,code", [("projection_epoch", "INVALID_SOURCE"), ("version", "LIMIT_EXCEEDED")]
)
def test_forged_epoch_or_exhausted_version_cannot_wrap_or_partially_apply(
    seeded_db, golden, field, code
):
    freeze_id = _freeze(seeded_db, golden)
    with seeded_db.connect("owner") as conn:
        if field == "projection_epoch":
            conn.execute("UPDATE ledger.workspace SET projection_epoch=%s", (2**63 - 1,))
        else:
            conn.execute(
                "UPDATE ledger.account_balance SET version=%s WHERE account_id=%s",
                (2**63 - 1, golden["accounts"][0]["id"]),
            )
    before = _state(seeded_db)
    with pytest.raises(MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze_id)
    # An epoch forged without its repair history is invalid source metadata,
    # whereas an exhausted legitimate account version is a repair limit.
    assert caught.value.code == code
    assert _state(seeded_db) == before


@pytest.mark.parametrize("outer", [False, True], ids=["autocommit-off", "outer-transaction"])
def test_maintenance_refuses_connection_state_that_could_return_before_outer_commit(
    seeded_db, golden, outer
):
    before = _state(seeded_db)
    with seeded_db.connect("maintenance") as conn:
        if outer:
            conn.execute("BEGIN")
        else:
            conn.autocommit = False
        with pytest.raises(MaintenanceError) as caught:
            freeze_workspace(conn, golden["workspace"]["id"], str(uuid4()), ACTOR, "test")
        assert caught.value.code == "IDLE_AUTOCOMMIT_REQUIRED"
        assert conn.info.transaction_status == (
            TransactionStatus.INTRANS if outer else TransactionStatus.IDLE
        )
        conn.rollback()
    assert _state(seeded_db) == before


@pytest.mark.parametrize(
    "mutate_barrier", [False, True], ids=["real-barrier", "removed-barrier-mutant"]
)
def test_freeze_waits_for_real_inflight_purchase_and_next_purchase_is_rejected(
    seeded_db, golden, mutate_barrier
):
    principal, command, request = purchase_input(golden)
    workspace = golden["workspace"]["id"]
    skipped = []

    class NoSharedBarrier(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if isinstance(query, str) and "pg_advisory_xact_lock_shared" in query:
                skipped.append(query)
                return super().execute("SELECT 1")
            return super().execute(query, params, **kwargs)

    with (
        seeded_db.connect("owner") as blocker,
        seeded_db.connect("app") as writer,
        seeded_db.connect("maintenance") as maintenance,
        seeded_db.connect("admin") as observer,
    ):
        if mutate_barrier:
            writer.cursor_factory = NoSharedBarrier
        writer_worker = freeze_worker = None
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT account_id FROM ledger.account_balance WHERE account_id=%s FOR UPDATE",
                    (str(command.customer_account_id),),
                )
                writer_worker = start_call(
                    lambda: submit_purchase(writer, principal, command, request)
                )
                wait_for_blocker(
                    observer, writer.info.backend_pid, blocker.info.backend_pid, writer_worker
                )
                freeze_worker = start_call(
                    lambda: freeze_workspace(maintenance, workspace, str(uuid4()), ACTOR, "drain")
                )

                def original_drain_assertion():
                    wait_for_blocker(
                        observer,
                        maintenance.info.backend_pid,
                        writer.info.backend_pid,
                        freeze_worker,
                        query_fragment="pg_advisory_xact_lock",
                    )
                    assert observer.execute("SELECT mode FROM ledger.workspace").fetchone() == (
                        "NORMAL",
                    )

                if mutate_barrier:
                    with pytest.raises(AssertionError, match="call completed before"):
                        original_drain_assertion()
                else:
                    original_drain_assertion()
        finally:
            if writer_worker is not None:
                writer_kind, writer_value = finish_call(writer_worker)
            if freeze_worker is not None:
                freeze_kind, freeze_value = finish_call(freeze_worker)
        assert writer_kind == freeze_kind == "returned", (writer_value, freeze_value)
        assert writer_value.http_status == 201
        assert len(skipped) == int(mutate_barrier)
        assert observer.execute("SELECT mode FROM ledger.workspace").fetchone() == ("FROZEN",)
        assert observer.execute(
            "SELECT booked FROM ledger.account_balance WHERE account_id=%s",
            (str(command.customer_account_id),),
        ).fetchone() == (20,)
        before = _state(seeded_db)
        with pytest.raises(RetrySameIdentity) as caught:
            submit_purchase(
                writer,
                principal,
                replace(command, operation_id=OperationId(uuid4())),
                replace(request, key="after-freeze"),
            )
        assert caught.value.reason == "workspace_not_normal"
        assert _state(seeded_db) == before


@pytest.mark.parametrize("role", ["app", "reader"])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger.workspace SET projection_epoch=projection_epoch",
        "UPDATE ledger.workspace SET current_freeze_id=NULL",
        "UPDATE ledger.maintenance_freeze SET resumed_at=clock_timestamp()",
        "INSERT INTO ledger.projection_repair SELECT * FROM ledger.projection_repair",
        "INSERT INTO ledger.projection_repair_row SELECT * FROM ledger.projection_repair_row",
    ],
)
def test_business_and_export_credentials_cannot_forge_maintenance_metadata(
    seeded_db, role, statement
):
    with seeded_db.connect(role) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO ledger.operation SELECT * FROM ledger.operation",
        "UPDATE ledger.operation SET response_body=response_body",
        "DELETE FROM ledger.journal_entry",
        "UPDATE ledger.account SET enabled=false",
        "UPDATE ledger.account_balance SET reserved=0",
        "DELETE FROM ledger.account_balance",
        "UPDATE ledger.projection_repair SET reason=reason",
        "DELETE FROM ledger.projection_repair_row",
        "ALTER TABLE ledger.journal DISABLE TRIGGER ALL",
        "CREATE TABLE public.maintenance_bypass (id integer)",
        "CREATE TEMP TABLE maintenance_bypass (id integer)",
        "SET ROLE ledger_app",
        "SET ROLE ledger_migrator",
        "SET ROLE postgres",
    ],
)
def test_actual_maintenance_credential_has_no_history_ddl_or_reservation_authority(
    seeded_db, statement
):
    with seeded_db.connect("maintenance") as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ledger_maintenance",)
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)


FAULTS = {
    "balance": ("account_balance", "UPDATE", False),
    "audit": ("projection_repair_row", "INSERT", False),
    "epoch": ("workspace", "UPDATE", False),
    "commit": ("projection_repair", "INSERT", True),
}


def _fault_guard(db, stage, *, install):
    table, event, deferred = FAULTS[stage]
    with db.connect("owner") as conn, conn.transaction():
        if not install:
            conn.execute(
                sql.SQL("DROP TRIGGER test_repair_failure ON ledger.{}").format(
                    sql.Identifier(table)
                )
            )
            conn.execute("DROP FUNCTION ledger.test_repair_failure()")
            return
        conn.execute("""
            CREATE FUNCTION ledger.test_repair_failure() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
            BEGIN
              IF TG_ARGV[0] = 'commit' AND NOT EXISTS (
                SELECT 1 FROM ledger.workspace WHERE projection_epoch=2 AND mode='FROZEN'
              ) THEN
                RAISE EXCEPTION 'fault was not reached after the epoch write';
              END IF;
              RAISE EXCEPTION USING ERRCODE='23514',
                CONSTRAINT='test_repair_failure', MESSAGE='injected repair ' || TG_ARGV[0];
            END $$
        """)
        condition = (
            " WHEN (OLD.projection_epoch <> NEW.projection_epoch)" if stage == "epoch" else ""
        )
        conn.execute(
            sql.SQL(
                "CREATE {constraint}TRIGGER test_repair_failure AFTER {event} ON ledger.{table} "
                "{deferred}FOR EACH ROW{condition} "
                "EXECUTE FUNCTION ledger.test_repair_failure({stage})"
            ).format(
                constraint=sql.SQL("CONSTRAINT " if deferred else ""),
                event=sql.SQL(event),
                table=sql.Identifier(table),
                deferred=sql.SQL("DEFERRABLE INITIALLY DEFERRED " if deferred else ""),
                condition=sql.SQL(condition),
                stage=sql.Literal(stage),
            )
        )


@pytest.mark.parametrize("stage", FAULTS)
def test_repair_sql_failures_roll_back_balances_audit_and_epoch_then_same_id_recovers(
    seeded_db, golden, stage
):
    _drift(seeded_db, golden, missing_idle=True)
    freeze_id = _freeze(seeded_db, golden)
    repair_id, preview_id = str(uuid4()), str(uuid4())
    before = _state(seeded_db)
    _fault_guard(seeded_db, stage, install=True)
    try:
        with pytest.raises(errors.CheckViolation) as caught:
            _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
        assert caught.value.diag.constraint_name == "test_repair_failure"
        assert caught.value.diag.message_primary == "injected repair " + stage
        assert _state(seeded_db) == before
    finally:
        _fault_guard(seeded_db, stage, install=False)
    _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    _assert_money(seeded_db, [100, 20, 80, 0])
    after = _state(seeded_db)
    _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    assert _state(seeded_db) == after


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_ambiguous_repair_commit_recovers_with_same_repair_identity(seeded_db, golden, fault):
    _drift(seeded_db, golden)
    freeze_id = _freeze(seeded_db, golden)
    repair_id, preview_id = str(uuid4()), str(uuid4())
    before = _state(seeded_db)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        with psycopg.connect(
            host="127.0.0.1",
            port=proxy.port,
            dbname=seeded_db.name,
            user="ledger_maintenance",
            password=seeded_db.maintenance_password,
            autocommit=True,
            connect_timeout=3,
            sslmode="disable",
            gssencmode="disable",
        ) as conn:
            backend = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                apply_rebuild(
                    conn,
                    golden["workspace"]["id"],
                    freeze_id,
                    repair_id,
                    ACTOR,
                    "recompute from immutable entries",
                    preview_id,
                )
            assert conn.closed
        assert proxy.finished.wait(5)
        assert proxy.frontend_commit_seen.is_set() and proxy.fault_applied.is_set()
        assert proxy.commit_completions_forwarded == 0
        assert proxy.commit_queries_forwarded == int(fault == "after-commit")
        assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    _wait_backend_gone(seeded_db, backend)
    if fault == "before-forward":
        assert _state(seeded_db) == before
    else:
        _assert_money(seeded_db, [100, 20, 80, 0])
    result = _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    assert result["new_epoch"] == 2
    _assert_money(seeded_db, [100, 20, 80, 0])
    final = _state(seeded_db)
    _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    assert _state(seeded_db) == final
    assert len(final["projection_repair"]) == 1 and len(final["projection_repair_row"]) == 4


def test_freeze_identity_is_scoped_to_current_workspace(seeded_db, golden):
    freeze_id = _freeze(seeded_db, golden)
    other_workspace, other_freeze = str(uuid4()), str(uuid4())
    with seeded_db.connect("owner") as conn:
        conn.execute("INSERT INTO ledger.workspace (id) VALUES (%s)", (other_workspace,))
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, other_workspace, other_freeze, ACTOR, "other workspace")
        before = _state(seeded_db)
        with pytest.raises(MaintenanceError) as caught:
            apply_rebuild(
                conn, other_workspace, freeze_id, str(uuid4()), ACTOR, "wrong scope", str(uuid4())
            )
        assert caught.value.code == "FREEZE_CONFLICT"
        with pytest.raises(MaintenanceError) as caught:
            resume_workspace(conn, other_workspace, freeze_id, ACTOR, "wrong scope")
        assert caught.value.code == "FREEZE_CONFLICT"
        with pytest.raises(MaintenanceError) as caught:
            freeze_workspace(conn, golden["workspace"]["id"], str(uuid4()), ACTOR, "overwrite")
        assert caught.value.code == "FREEZE_CONFLICT"
    assert _state(seeded_db) == before


def test_committed_repair_audit_cannot_be_extended_with_a_later_account(seeded_db, golden):
    freeze_id = _freeze(seeded_db, golden)
    result = _apply(seeded_db, golden, freeze_id)
    extra_account = str(uuid4())
    workspace = golden["workspace"]["id"]
    # Owner-only setup adds a legitimate new zero-activity account later. The
    # historical repair still declares four accounts, not today's entire set.
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "INSERT INTO ledger.account (workspace_id,id,owner_id,class,normal_side) "
            "VALUES (%s,%s,%s,'MERCHANT_LIABILITY','C')",
            (workspace, extra_account, str(uuid4())),
        )
    before = _state(seeded_db)
    with seeded_db.connect("maintenance") as conn:
        with pytest.raises(errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO ledger.projection_repair_row "
                    "(workspace_id,repair_id,account_id,normal_side,after_debits,after_credits,"
                    "after_reserved,after_booked,after_version) VALUES (%s,%s,%s,'C',0,0,0,0,1)",
                    (workspace, result["repair_id"], extra_account),
                )
                # The attempted extra audit row actually exists inside this
                # transaction; only the deferred declaration check rejects COMMIT.
                assert conn.execute(
                    "SELECT count(*) FROM ledger.projection_repair_row"
                ).fetchone() == (5,)
        assert conn.info.transaction_status == TransactionStatus.IDLE
    assert _state(seeded_db) == before


def test_stale_preview_target_mutation_is_rejected_by_independent_postcheck(
    seeded_db, golden, tmp_path, monkeypatch
):
    _drift(seeded_db, golden)
    freeze_id = _freeze(seeded_db, golden)
    preview = _preview(seeded_db, golden, freeze_id, tmp_path)
    preview_id, repair_id = Path(preview["export_path"]).name, str(uuid4())
    # Explicit privileged setup changes the true journal source after the preview.
    # The mutant then substitutes the old targets for the freshly computed values.
    fund_customer(seeded_db, golden, 10)
    before = _state(seeded_db)
    fresh_targets = maintenance._targets
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, "_targets", lambda *args: preview["changes"])
        with pytest.raises(MaintenanceError) as caught:
            _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
        assert caught.value.code == "PROJECTION_DRIFT"
        assert any(issue["code"] == "PROJECTION_DRIFT" for issue in caught.value.report["issues"])
    assert maintenance._targets is fresh_targets
    assert _state(seeded_db) == before
    _apply(seeded_db, golden, freeze_id, repair_id=repair_id, preview_id=preview_id)
    _assert_money(seeded_db, [110, 30, 80, 0])


def _observed_maintenance_race(db, workspace, first_call, second_call):
    """Queue both real calls behind an observed shared workspace lock, then release."""
    with (
        db.connect("admin") as blocker,
        db.connect("maintenance") as first,
        db.connect("maintenance") as second,
        db.connect("admin") as observer,
    ):
        first_worker = second_worker = None
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT pg_advisory_xact_lock_shared("
                    "hashtextextended('ledgerops:workspace:' || %s::text,0))",
                    (workspace,),
                )
                first_worker = start_call(lambda: first_call(first))
                wait_for_blocker(
                    observer,
                    first.info.backend_pid,
                    blocker.info.backend_pid,
                    first_worker,
                    query_fragment="pg_advisory_xact_lock",
                )
                second_worker = start_call(lambda: second_call(second))
                wait_for_blocker(
                    observer,
                    second.info.backend_pid,
                    (blocker.info.backend_pid, first.info.backend_pid),
                    second_worker,
                    query_fragment="pg_advisory_xact_lock",
                )
        finally:
            if first_worker is not None:
                first_outcome = finish_call(first_worker)
            if second_worker is not None:
                second_outcome = finish_call(second_worker)
        assert (
            first.info.transaction_status
            == second.info.transaction_status
            == TransactionStatus.IDLE
        )
    return first_outcome, second_outcome


def test_competing_freeze_ids_serialize_without_overwriting_the_winner(seeded_db, golden):
    workspace = golden["workspace"]["id"]
    first_id, second_id = str(uuid4()), str(uuid4())
    before = _state(seeded_db)
    outcomes = _observed_maintenance_race(
        seeded_db,
        workspace,
        lambda conn: freeze_workspace(conn, workspace, first_id, ACTOR, "first"),
        lambda conn: freeze_workspace(conn, workspace, second_id, ACTOR, "second"),
    )
    accepted = [value for kind, value in outcomes if kind == "returned"]
    rejected = [value for kind, value in outcomes if kind == "raised"]
    assert len(accepted) == len(rejected) == 1, outcomes
    assert isinstance(rejected[0], MaintenanceError) and rejected[0].code == "FREEZE_CONFLICT"
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT mode,current_freeze_id FROM ledger.workspace").fetchone() == (
            "FROZEN",
            UUID(accepted[0]["freeze_id"]),
        )
        assert conn.execute("SELECT count(*) FROM ledger.maintenance_freeze").fetchone() == (1,)
    after = _state(seeded_db)
    for table in before.keys() - {"workspace", "maintenance_freeze"}:
        assert after[table] == before[table]


def test_same_repair_id_race_replays_one_atomic_effect(seeded_db, golden):
    _drift(seeded_db, golden, missing_idle=True)
    freeze_id = _freeze(seeded_db, golden)
    workspace, repair_id, preview_id = golden["workspace"]["id"], str(uuid4()), str(uuid4())

    def apply(conn):
        return apply_rebuild(
            conn, workspace, freeze_id, repair_id, ACTOR, "one raced repair", preview_id
        )

    outcomes = _observed_maintenance_race(seeded_db, workspace, apply, apply)
    assert all(kind == "returned" for kind, _ in outcomes), outcomes
    assert outcomes[0][1] == outcomes[1][1]
    assert outcomes[0][1]["new_epoch"] == 2
    _assert_money(seeded_db, [100, 20, 80, 0])
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT projection_epoch FROM ledger.workspace").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM ledger.projection_repair").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM ledger.projection_repair_row").fetchone() == (4,)
