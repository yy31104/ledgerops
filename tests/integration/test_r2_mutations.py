"""Defect-detection experiments distinguish business outcomes from SQL safeguards.

Mutants run only in disposable databases or per-test cursors/checker patches. A
named SQL failure is never presented as the normal successful business outcome:
the same literal business oracle must reject it before its safety bound is checked.
"""

from dataclasses import replace
from pathlib import Path
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.pq import TransactionStatus

from ledgerops.db import maintenance
from ledgerops.db.admission import submit_reversal
from ledgerops.db.maintenance import MaintenanceError
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.concurrency_helpers import finish_call, start_call
from tests.integration.purchase_helpers import fund_customer, invoke, purchase_input
from tests.integration.reversal_helpers import (
    assert_balances,
    original_rows,
    reversal_input,
    reversal_state,
    reverse,
)

pytestmark = pytest.mark.integration


def _outcome(call):
    try:
        return "returned", call()
    except Exception as error:
        return "raised", error


def _assert_new_success(outcome):
    assert outcome[0] == "returned", "new eligible reversal must return a business success"
    assert outcome[1].http_status == 201 and not outcome[1].replayed


def _assert_normal_race(outcomes):
    assert all(kind == "returned" for kind, _ in outcomes), (
        "normal reversal race must return two business results"
    )
    assert [value.http_status for _, value in outcomes] == [201, 422]
    assert outcomes[1][1].body["error"]["code"] == "ALREADY_REVERSED"


def test_all_attempt_uniqueness_mutant_breaks_rejected_then_new_success(seeded_db, golden):
    # Frozen literal case: P80; M80+60=140 with reserve100 refuses RA80;
    # M+40=180 makes a distinct RB eligible, leaving C100/M100/reserve100.
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    merchant = golden["purchase_request"]["merchant_account_id"]
    fund_customer(seeded_db, golden, 60, customer_account_id=merchant)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=100 WHERE account_id=%s", (merchant,)
        )
    rejected_args = reversal_input(golden, key="bad-unique-ra")
    rejected = reverse(seeded_db, *rejected_args)
    assert rejected.http_status == 422
    assert rejected.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "ALTER TABLE ledger.reversal_attempt ADD CONSTRAINT test_all_attempt_original_unique "
            "UNIQUE (workspace_id,original_operation_id)"
        )
    fresh_args = reversal_input(golden, key="bad-unique-rb")
    try:
        fund_customer(seeded_db, golden, 40, customer_account_id=merchant)
        assert reverse(seeded_db, *rejected_args).body == rejected.body
        before = reversal_state(seeded_db)
        mutant = _outcome(lambda: reverse(seeded_db, *fresh_args))
        with pytest.raises(AssertionError, match="new eligible reversal"):
            _assert_new_success(mutant)
        assert mutant[0] == "raised" and isinstance(mutant[1], errors.UniqueViolation)
        assert mutant[1].diag.constraint_name == "test_all_attempt_original_unique"
        assert reversal_state(seeded_db) == before
    finally:
        with seeded_db.connect("owner") as conn:
            conn.execute(
                "ALTER TABLE ledger.reversal_attempt "
                "DROP CONSTRAINT test_all_attempt_original_unique"
            )
    # The exact same business identity now reaches the intended business assertion.
    _assert_new_success(_outcome(lambda: reverse(seeded_db, *fresh_args)))
    assert reverse(seeded_db, *rejected_args).body == rejected.body
    assert_balances(seeded_db, golden, [200, 100, 100, 0])
    assert original_rows(seeded_db, original_id) == original
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.reversal_attempt").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM ledger.purchase_reversal_effect").fetchone() == (
            1,
        )


def test_removing_original_serialization_breaks_business_race_but_success_pk_still_limits_effect(
    seeded_db, golden
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    # Both stale decisions remain financially eligible even after the first80 debit.
    fund_customer(
        seeded_db, golden, 80, customer_account_id=golden["purchase_request"]["merchant_account_id"]
    )
    arguments = [
        reversal_input(golden, key="no-original-lock-a"),
        reversal_input(golden, key="no-original-lock-b"),
    ]
    arrived = [Event(), Event()]
    release = [Event(), Event()]
    removed = [[], []]
    original_query = (
        "SELECT * FROM ledger.operation WHERE workspace_id=%s AND operation_id=%s FOR UPDATE"
    )
    success_query = (
        "SELECT reversal_operation_id FROM ledger.purchase_reversal_effect "
        "WHERE workspace_id=%s AND original_operation_id=%s"
    )

    def cursor_type(index):
        class MutantCursor(psycopg.Cursor):
            def execute(self, query, params=None, **kwargs):
                if query == original_query:
                    query = query.removesuffix(" FOR UPDATE")
                    removed[index].append(True)
                result = super().execute(query, params, **kwargs)
                if query == success_query:
                    assert self.rowcount == 0, "both decisions must actually observe no success"
                    arrived[index].set()
                    assert release[index].wait(5), (
                        "test did not release the observed stale decision"
                    )
                return result

        return MutantCursor

    workers = []
    with (
        seeded_db.connect("app") as first,
        seeded_db.connect("app") as second,
        seeded_db.connect("admin") as observer,
    ):
        first.cursor_factory, second.cursor_factory = cursor_type(0), cursor_type(1)
        connections = [first, second]
        try:
            for index in range(2):
                workers.append(
                    start_call(
                        lambda index=index: submit_reversal(connections[index], *arguments[index])
                    )
                )
                assert arrived[index].wait(3), "reversal never reached its success observation"
            for index, conn in enumerate(connections):
                state, query = observer.execute(
                    "SELECT state,query FROM pg_stat_activity WHERE pid=%s",
                    (conn.info.backend_pid,),
                ).fetchone()
                assert state == "idle in transaction"
                assert "SELECT reversal_operation_id FROM ledger.purchase_reversal_effect" in query
                assert "original_operation_id=$2" in query
                assert removed[index] == [True]
            release[0].set()
            first_outcome = finish_call(workers[0])
            _assert_new_success(first_outcome)
            after_first = reversal_state(seeded_db)
            release[1].set()
            second_outcome = finish_call(workers[1])
        finally:
            for event in release:
                event.set()
            for worker in workers:
                if not worker.done.is_set():
                    finish_call(worker)
        assert (
            first.info.transaction_status
            == second.info.transaction_status
            == TransactionStatus.IDLE
        )
    mutant = [first_outcome, second_outcome]
    with pytest.raises(AssertionError, match="two business results"):
        _assert_normal_race(mutant)
    assert second_outcome[0] == "raised" and isinstance(second_outcome[1], errors.UniqueViolation)
    assert second_outcome[1].diag.constraint_name == "purchase_reversal_effect_pkey"
    # The second transaction's journal, projection updates, attempt and claims roll back.
    assert reversal_state(seeded_db) == after_first
    assert_balances(seeded_db, golden, [180, 100, 80, 0])
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.purchase_reversal_effect").fetchone() == (
            1,
        )
        assert conn.execute("SELECT count(*) FROM ledger.reversal_attempt").fetchone() == (1,)
        assert verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"])).healthy
    assert original_rows(seeded_db, original_id) == original
    normal = [first_outcome, _outcome(lambda: reverse(seeded_db, *arguments[1]))]
    _assert_normal_race(normal)
    assert_balances(seeded_db, golden, [180, 100, 80, 0])


def test_omitting_live_intent_check_mutant_can_launder_projection_but_independent_b_detects_it(
    seeded_db, golden, tmp_path, monkeypatch
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    workspace = golden["workspace"]["id"]
    freeze, repair, actor = (str(uuid4()) for _ in range(3))
    with seeded_db.connect("maintenance") as conn:
        maintenance.freeze_workspace(conn, workspace, freeze, actor, "inspect source")
        preview = maintenance.preview_rebuild(
            conn, workspace, freeze, tmp_path, code_revision="0" * 40
        )
    assert preview["report"]["rebuild_eligible"]
    request = golden["purchase_request"]
    wrong = golden["accounts"][3]["id"]
    # Deliberate privileged corruption after the preview: canonical request remains M1.
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.journal_entry SET account_id=%s "
            "WHERE workspace_id=%s AND account_id=%s AND side='C'",
            (wrong, workspace, request["merchant_account_id"]),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")

    def apply():
        with seeded_db.connect("maintenance") as conn:
            return maintenance.apply_rebuild(
                conn,
                workspace,
                freeze,
                repair,
                actor,
                "repair source",
                Path(preview["export_path"]).name,
            )

    def assert_intent_gate(outcome):
        assert outcome[0] == "raised", "wrong-owner source must be refused before repair"
        assert isinstance(outcome[1], MaintenanceError) and outcome[1].code == "INVALID_SOURCE"
        assert "WRONG_OWNER" in {issue["code"] for issue in outcome[1].report["issues"]}

    before = reversal_state(seeded_db)
    assert_intent_gate(_outcome(apply))
    assert reversal_state(seeded_db) == before
    original_checker = maintenance.verify_snapshot
    with monkeypatch.context() as patch:
        patch.setattr(
            maintenance,
            "verify_snapshot",
            lambda snapshot: replace(original_checker(snapshot), request_intent_ok=True),
        )
        mutant = _outcome(apply)
        with pytest.raises(AssertionError, match="wrong-owner source must be refused"):
            assert_intent_gate(mutant)
        assert mutant[0] == "returned" and mutant[1]["new_epoch"] == 2
    # The unpatched independent checker still exposes the bad source even though
    # the mutant has made its projection agree and recorded a completed repair.
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, workspace))
        assert conn.execute("SELECT mode,projection_epoch FROM ledger.workspace").fetchone() == (
            "FROZEN",
            2,
        )
    assert report.structure_ok and report.projection_ok and not report.request_intent_ok
    assert "WRONG_OWNER" in {issue.code for issue in report.issues}
    assert not report.rebuild_eligible
    assert_balances(seeded_db, golden, [100, 20, 0, 80])
