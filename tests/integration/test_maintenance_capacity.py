"""Real large-source maintenance under the existing exclusive write barrier."""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from ledgerops.db import maintenance
from ledgerops.db.payout_resolution import receive_observation
from ledgerops.export import ExportLimitExceeded, capture_snapshot
from ledgerops.maintenance_snapshot import CAPTURE_CONTEXT, assess_current
from ledgerops.reconciliation.statement import CorruptInput
from ledgerops.stream_reconciliation import build_compact
from ledgerops.stream_verification import StreamError, verify_stream
from tests.integration.payout_helpers import admit, payout_input, payout_state
from tests.integration.payout_observation_helpers import prepared_payout, trust
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.test_maintenance import ACTOR, _apply, _freeze, _state
from tests.integration.test_reconciliation_publication import REVISION

pytestmark = pytest.mark.integration


def big_source(database, golden):
    first = prepared_payout(database, golden)
    second = payout_input(
        golden, key="maintenance-small", operation_id=str(uuid4()), principal_minor="1"
    )
    assert admit(database, golden, second).http_status == 202
    with database.connect("worker") as conn:
        for number in range(129):
            command = first if number < 65 else second[1]
            raw = number.to_bytes(2, "big") + b"x" * 65534
            assert (
                receive_observation(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    raw,
                    trust=trust(command),
                    channel="CALLBACK",
                ).status
                == "QUARANTINED"
            )
    with database.connect("owner") as conn:
        conn.execute(
            "INSERT INTO ledger.request_result(workspace_id,client_id,route_family,key,"
            "operation_id,payload,payload_sha256,http_status,response_body) "
            "SELECT workspace_id,client_id,route_family,'capacity-'||g::text,operation_id,"
            "payload,payload_sha256,http_status,response_body FROM ledger.request_result "
            "CROSS JOIN generate_series(1,10005) g WHERE operation_id=%s",
            (str(first.operation_id),),
        )
    return first, second[1]


def preview(database, golden, freeze, root):
    with database.connect("maintenance") as conn:
        return maintenance.preview_rebuild(
            conn, golden["workspace"]["id"], freeze, root, code_revision=REVISION, dirty=True
        )


def test_large_preview_repair_resume_preserves_all_history_and_active_holds(
    seeded_db, golden, tmp_path
):
    big_source(seeded_db, golden)
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect("reader") as conn:
        with pytest.raises(ExportLimitExceeded):
            capture_snapshot(conn, golden["workspace"]["id"])
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=90,reserved=12 WHERE account_id=%s",
            (merchant,),
        )
    freeze = _freeze(seeded_db, golden)
    before = payout_state(seeded_db)
    item = preview(seeded_db, golden, freeze, tmp_path)
    report = item["report"]
    assert report["report_version"] == 4 and report["capture_context"] == CAPTURE_CONTEXT
    assert (
        report["structure_ok"] and report["request_intent_ok"] and report["reservation_supported"]
    )
    assert not report["projection_ok"] and report["rebuild_eligible"]
    path = Path(item["export_path"])
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["source"]["capture_context"] == CAPTURE_CONTEXT
    assert manifest["row_counts"]["observation_quarantines"] == 129
    assert manifest["row_counts"]["requests"] > 10000
    assert verify_stream(path) == report
    with pytest.raises(CorruptInput, match="REQUIRES_REPEATABLE_READ"):
        build_compact(path)
    result = _apply(seeded_db, golden, freeze, preview_id=path.name)
    assert result["old_epoch"] == 1 and result["new_epoch"] == 2
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked,reserved FROM ledger.account_balance WHERE account_id=%s", (merchant,)
        ).fetchone() == (80, 65)
        assert conn.execute(
            "SELECT before_reserved,after_reserved FROM ledger.projection_repair_row "
            "WHERE account_id=%s",
            (merchant,),
        ).fetchone() == (12, 65)
    after = payout_state(seeded_db)
    for name in (
        "payout",
        "reservation",
        "reservation_event",
        "dispatch_intent",
        "transport_attempt",
        "journal",
        "journal_entry",
        "operation",
        "request_result",
    ):
        assert after[name] == before[name]
    with seeded_db.connect("maintenance") as conn:
        resumed = maintenance.resume_workspace(
            conn, golden["workspace"]["id"], freeze, ACTOR, "reviewed full source"
        )
        saved = resumed["resume_report"]
        assert saved["report_version"] == 4 and saved["verification_complete"]
        assert saved["capture_context"] == CAPTURE_CONTEXT
        assert saved["source_export_id"] is None and saved["source_manifest_sha256"] is None
        assert saved["structure_ok"] and saved["projection_ok"] and saved["request_intent_ok"]
        assert (
            maintenance.resume_workspace(
                conn, golden["workspace"]["id"], freeze, ACTOR, "reviewed full source"
            )
            == resumed
        )
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT mode,current_freeze_id FROM ledger.workspace").fetchone() == (
            "NORMAL",
            None,
        )


def test_large_wrong_owner_blocks_apply_but_a_only_resume_retains_v4_warning(
    seeded_db, golden, tmp_path
):
    big_source(seeded_db, golden)
    extra = purchase_input(golden, key="extra20", operation_id=str(uuid4()), amount_minor="20")
    assert invoke(seeded_db, *extra).http_status == 201
    request = golden["purchase_request"]
    wrong = golden["accounts"][3]["id"]
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("ALTER TABLE ledger.journal_entry DISABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.journal_entry SET account_id=%s WHERE journal_id="
            "(SELECT journal_id FROM ledger.journal WHERE operation_id=%s) AND side='C'",
            (wrong, str(extra[1].operation_id)),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
        conn.execute("ALTER TABLE ledger.journal_entry ENABLE TRIGGER journal_entry_immutable")
        conn.execute(
            "UPDATE ledger.account_balance SET credits=80 WHERE account_id=%s",
            (request["merchant_account_id"],),
        )
        conn.execute("UPDATE ledger.account_balance SET credits=20 WHERE account_id=%s", (wrong,))
    freeze = _freeze(seeded_db, golden)
    item = preview(seeded_db, golden, freeze, tmp_path)
    assert item["report"]["structure_ok"] and item["report"]["projection_ok"]
    assert not item["report"]["request_intent_ok"] and item["changes"] == []
    before = _state(seeded_db)
    with pytest.raises(maintenance.MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze)
    assert caught.value.code == "INVALID_SOURCE"
    assert "WRONG_OWNER" in {row["code"] for row in caught.value.report["issues"]}
    assert _state(seeded_db) == before
    with seeded_db.connect("maintenance") as conn:
        resumed = maintenance.resume_workspace(
            conn, golden["workspace"]["id"], freeze, ACTOR, "acknowledged B warning"
        )
    saved = resumed["resume_report"]
    assert saved["structure_ok"] and saved["projection_ok"] and not saved["request_intent_ok"]
    assert saved["verification_complete"] and saved["capture_context"] == CAPTURE_CONTEXT
    assert "WRONG_OWNER" in {row["code"] for row in saved["issues"]}


def test_large_postcheck_mutation_rolls_back_balances_epoch_and_audit(
    seeded_db, golden, monkeypatch
):
    big_source(seeded_db, golden)
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=90,reserved=12 WHERE account_id=%s",
            (merchant,),
        )
    freeze = _freeze(seeded_db, golden)
    before = _state(seeded_db)
    original = maintenance._write_projection

    def omit_reserved(cur, workspace, repair, change, **kwargs):
        if change["account_id"] == merchant:
            change = copy.deepcopy(change)
            change["after"]["reserved"] = "12"
        return original(cur, workspace, repair, change, **kwargs)

    monkeypatch.setattr(maintenance, "_write_projection", omit_reserved)
    with pytest.raises(maintenance.MaintenanceError) as caught:
        _apply(seeded_db, golden, freeze)
    assert caught.value.code == "PROJECTION_DRIFT"
    assert _state(seeded_db) == before


def test_non_ascii_sqlite_body_budget_counts_bytes_before_semantic_decode(seeded_db, golden):
    big_source(seeded_db, golden)
    with seeded_db.connect("admin") as conn:
        conn.execute("ALTER TABLE ledger.operation DISABLE TRIGGER USER")
        try:
            conn.execute(
                "UPDATE ledger.operation SET response_body=jsonb_build_object('unicode',%s::text) "
                "WHERE kind='OPENING'",
                ("\u6c49" * 10000,),
            )
        finally:
            conn.execute("ALTER TABLE ledger.operation ENABLE TRIGGER USER")
    freeze = _freeze(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        with maintenance._transaction(conn, golden["workspace"]["id"]) as (cur, workspace):
            with assess_current(cur, golden["workspace"]["id"]) as view:
                raw = view.store.conn.execute(
                    "SELECT body FROM operations WHERE kind='OPENING'"
                ).fetchone()[0]
                assert raw.isascii() and len(raw) == len(raw.encode("utf-8"))
                assert len(raw) > 60000
                with pytest.raises(StreamError, match="SEMANTIC_CLOSURE_BUDGET"):
                    view.store.collect("operations", "kind=?", ("OPENING",), budget=40000)
    assert freeze


def test_large_resume_waits_then_checks_fresh_post_commit_projection(seeded_db, golden):
    import psycopg

    from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker

    big_source(seeded_db, golden)
    merchant = golden["purchase_request"]["merchant_account_id"]
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=90,reserved=12 WHERE account_id=%s",
            (merchant,),
        )
    freeze = _freeze(seeded_db, golden)
    queries = []

    class ObservedCursor(psycopg.Cursor):
        def execute(self, query, *args, **kwargs):
            queries.append(str(query))
            return super().execute(query, *args, **kwargs)

    with (
        seeded_db.connect("owner") as blocker,
        seeded_db.connect("maintenance") as worker,
        seeded_db.connect("admin") as observer,
    ):
        worker.cursor_factory = ObservedCursor
        pending = None
        try:
            with blocker.transaction():
                blocker.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"ledgerops:workspace:{golden['workspace']['id']}",),
                )
                blocker.execute(
                    "UPDATE ledger.account_balance SET credits=80,reserved=65,version=version+1 "
                    "WHERE account_id=%s",
                    (merchant,),
                )
                pending = start_call(
                    lambda: maintenance.resume_workspace(
                        worker, golden["workspace"]["id"], freeze, ACTOR, "fresh after barrier wait"
                    )
                )
                wait_for_blocker(
                    observer,
                    worker.info.backend_pid,
                    blocker.info.backend_pid,
                    pending,
                    query_fragment="pg_advisory_xact_lock",
                )
        finally:
            if pending is not None:
                outcome, result = finish_call(pending)
        assert outcome == "returned", result
        assert (
            result["resume_report"]["projection_ok"]
            and result["resume_report"]["verification_complete"]
        )
    assert not any("REPEATABLE READ" in query for query in queries)
    assert any("READ COMMITTED READ WRITE" in query for query in queries)


def test_incomplete_independent_review_cannot_repair_or_resume_large_source(
    seeded_db, golden, monkeypatch
):
    from ledgerops import stream_verification

    big_source(seeded_db, golden)
    freeze = _freeze(seeded_db, golden)
    before = _state(seeded_db)
    monkeypatch.setattr(stream_verification, "MAX_COHORT", 64)
    with pytest.raises(maintenance.MaintenanceError) as apply_error:
        _apply(seeded_db, golden, freeze)
    assert apply_error.value.code == "VERIFICATION_INCOMPLETE"
    with seeded_db.connect("maintenance") as conn:
        with pytest.raises(maintenance.MaintenanceError) as resume_error:
            maintenance.resume_workspace(
                conn, golden["workspace"]["id"], freeze, ACTOR, "must stay frozen"
            )
    assert resume_error.value.code == "VERIFICATION_INCOMPLETE"
    assert resume_error.value.report["verification_complete"] is False
    assert resume_error.value.report["unverified_scope_count"] > 0
    assert _state(seeded_db) == before


def test_private_capture_requires_existing_exclusive_read_committed_barrier(seeded_db, golden):
    from psycopg.rows import dict_row

    from ledgerops.export import ExportError

    with seeded_db.connect("maintenance") as conn:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
            with pytest.raises(ExportError, match="exclusive workspace barrier"):
                with assess_current(cur, golden["workspace"]["id"]):
                    pass
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ WRITE")
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"ledgerops:workspace:{golden['workspace']['id']}",),
            )
            with pytest.raises(ExportError, match="existing READ COMMITTED"):
                with assess_current(cur, golden["workspace"]["id"]):
                    pass
