"""Physical-index upgrade preserves financial formats and closed restores."""

import json
from importlib.resources import files

import psycopg
import pytest

from ledgerops.backup import BackupError
from ledgerops.db.migrations import (
    INDEX_ONLY_MIGRATION,
    apply_index_only_migration,
    apply_migrations,
)
from ledgerops.export import ExportError, capture_snapshot
from ledgerops.stream_export import export_stream
from ledgerops.stream_verification import verify_stream
from ledgerops.verification import verify_snapshot
from tests.integration.test_backup_recovery import drop, restore, source_backup
from tests.integration.test_backup_recovery import pg_tools as pg_tools

pytestmark = pytest.mark.integration


def remove_index_migration(db):
    with db.connect("owner") as conn, conn.transaction():
        conn.execute("DROP INDEX ledger.reservation_active_workspace_idx")
        conn.execute(
            "DELETE FROM ledger.schema_migration WHERE version=%s", (INDEX_ONLY_MIGRATION,)
        )


def test_index_only_upgrade_keeps_financial_rows_and_offline_formats(seeded_db, golden, tmp_path):
    remove_index_migration(seeded_db)
    with seeded_db.connect("reader") as conn:
        before = capture_snapshot(conn, golden["workspace"]["id"])
    with seeded_db.connect("admin") as conn:
        assert apply_migrations(conn) == [INDEX_ONLY_MIGRATION]
        assert apply_migrations(conn) == []
    with seeded_db.connect("reader") as conn:
        after = capture_snapshot(conn, golden["workspace"]["id"])
        stream = export_stream(
            conn,
            golden["workspace"]["id"],
            tmp_path,
            code_revision="9af388f5bd77f6a44740819cc31412cabc3a830e",
            dirty=True,
        )
    after_witness = after.pop("snapshot_witness")
    assert before.pop("snapshot_witness")
    assert before == after
    assert after["format_version"] == 9
    assert after["schema_versions"][-1] == "010_operator_actions.sql"
    assert verify_snapshot({**after, "snapshot_witness": after_witness}).healthy
    assert verify_stream(stream)["healthy"]
    manifest = json.loads((stream / "manifest.json").read_bytes())
    assert manifest["source"]["schema_versions"] == after["schema_versions"]


@pytest.mark.parametrize("fault", ["checksum", "future", "missing_financial_predecessor"])
def test_only_exact_index_capability_can_be_omitted_from_financial_schema(seeded_db, golden, fault):
    with seeded_db.connect("owner") as conn:
        if fault == "checksum":
            conn.execute(
                "UPDATE ledger.schema_migration SET sha256='wrong' WHERE version=%s",
                (INDEX_ONLY_MIGRATION,),
            )
        elif fault == "future":
            conn.execute(
                "INSERT INTO ledger.schema_migration(version,sha256) "
                "VALUES ('012_unknown.sql','unknown')"
            )
        else:
            conn.execute(
                "DELETE FROM ledger.schema_migration WHERE version='010_operator_actions.sql'"
            )
    with (
        seeded_db.connect("reader") as conn,
        pytest.raises(ExportError, match="Unsupported database schema"),
    ):
        capture_snapshot(conn, golden["workspace"]["id"])


@pytest.mark.parametrize("with_index", [False, True])
def test_actual_backup_retains_complete_schema_and_exact_old_backup_stays_closed(
    seeded_db, golden, pg_tools, tmp_path, with_index
):
    if not with_index:
        remove_index_migration(seeded_db)
    manifest = source_backup(seeded_db, pg_tools, tmp_path)
    assert len(manifest["schema"]) == (11 if with_index else 10)
    restored, _ = restore(seeded_db, pg_tools, tmp_path)
    try:
        with restored.connect("admin") as conn:
            assert conn.execute("SELECT count(*) FROM ledger.schema_migration").fetchone() == (
                11 if with_index else 10,
            )
            assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("RESTORE",)
            assert conn.execute(
                "SELECT has_database_privilege('ledger_app',current_database(),'CONNECT')"
            ).fetchone() == (False,)
            assert conn.execute(
                "SELECT to_regclass('ledger.reservation_active_workspace_idx') IS NOT NULL"
            ).fetchone() == (with_index,)
            assert verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"])).healthy
            if not with_index:
                before_workspace = conn.execute("SELECT * FROM ledger.workspace").fetchall()
                before_auth = conn.execute("SELECT * FROM ledger.auth_workspace_state").fetchall()
                assert apply_index_only_migration(conn) == [INDEX_ONLY_MIGRATION]
                assert apply_index_only_migration(conn) == []
                assert conn.execute("SELECT * FROM ledger.workspace").fetchall() == before_workspace
                assert (
                    conn.execute("SELECT * FROM ledger.auth_workspace_state").fetchall()
                    == before_auth
                )
                assert (
                    conn.execute(
                        "SELECT has_database_privilege(role,current_database(),'CONNECT') "
                        "FROM unnest(ARRAY['ledger_app','ledger_reader','ledger_worker',"
                        "'ledger_operator']) role"
                    ).fetchall()
                    == [(False,)] * 4
                )
                assert conn.execute("SELECT count(*) FROM ledger.schema_migration").fetchone() == (
                    11,
                )
        with pytest.raises(psycopg.OperationalError):
            with restored.connect("app"):
                pytest.fail("restored runtime unexpectedly connected")
    finally:
        drop(restored)


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def test_restore_does_not_accept_an_arbitrary_older_schema_prefix(seeded_db, pg_tools, tmp_path):
    remove_index_migration(seeded_db)
    with seeded_db.connect("owner") as conn:
        conn.execute("DELETE FROM ledger.schema_migration WHERE version='010_operator_actions.sql'")
    manifest = source_backup(seeded_db, pg_tools, tmp_path)
    assert len(manifest["schema"]) == 9
    with pytest.raises(BackupError, match="RESTORE_REQUIRES_EXACT_INSTALLED_SCHEMA"):
        restore(seeded_db, pg_tools, tmp_path)


@pytest.mark.parametrize("fault", ["checksum", "future", "prefix"])
def test_explicit_index_upgrade_rejects_unexpected_predecessor(db, fault):
    remove_index_migration(db)
    with db.connect("owner") as conn:
        if fault == "checksum":
            conn.execute(
                "UPDATE ledger.schema_migration SET sha256='wrong' "
                "WHERE version='010_operator_actions.sql'"
            )
        elif fault == "future":
            conn.execute(
                "INSERT INTO ledger.schema_migration(version,sha256) "
                "VALUES ('012_future.sql','future')"
            )
        else:
            conn.execute(
                "DELETE FROM ledger.schema_migration WHERE version='010_operator_actions.sql'"
            )
    with db.connect("admin") as conn, pytest.raises(ValueError, match="exact predecessor"):
        apply_index_only_migration(conn)
    with db.connect("reader") as conn:
        assert conn.execute(
            "SELECT to_regclass('ledger.reservation_active_workspace_idx')"
        ).fetchone() == (None,)


def test_runtime_cannot_apply_index_only_upgrade(db):
    with db.connect("app") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        apply_index_only_migration(conn)


def test_default_planner_uses_partial_covering_index_for_mostly_terminal_history(seeded_db, golden):
    workspace = golden["workspace"]["id"]
    merchant = golden["purchase_request"]["merchant_account_id"]
    query = (
        "SELECT coalesce(sum(gross_minor),0) FROM ledger.reservation "
        "WHERE workspace_id=%s AND state='HELD'"
    )
    with seeded_db.connect("admin") as conn, conn.transaction():
        # Isolated planner fixture only: bypass graph triggers for50k terminal
        # physical rows, never claim they are valid historical business evidence.
        # The transaction rolls back all fixture rows/index changes afterward.
        conn.execute("SET LOCAL session_replication_role=replica")
        conn.execute(
            "INSERT INTO ledger.reservation "
            "(workspace_id,payout_id,account_id,gross_minor,state,version) "
            "SELECT %s,md5('index-probe-'||g::text)::uuid,%s,3,"
            "CASE WHEN g<=50000 THEN 'RELEASED' ELSE 'HELD' END,"
            "CASE WHEN g<=50000 THEN 2 ELSE 1 END FROM generate_series(1,50008) g",
            (workspace, merchant),
        )
        conn.execute("SET LOCAL session_replication_role=origin")
        conn.execute("DROP INDEX ledger.reservation_active_workspace_idx")
        conn.execute("ANALYZE ledger.reservation")
        before = conn.execute(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, (workspace,)
        ).fetchone()[0][0]
        conn.execute(files("ledgerops.db").joinpath("sql", INDEX_ONLY_MIGRATION).read_text())
        after = conn.execute(
            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, (workspace,)
        ).fetchone()[0][0]
        assert conn.execute("SHOW enable_seqscan").fetchone() == ("on",)
        assert conn.execute(query, (workspace,)).fetchone() == (24,)
        nodes = list(_nodes(after["Plan"]))
        assert any(node.get("Index Name") == "reservation_active_workspace_idx" for node in nodes)
        assert sum(node.get("Actual Rows", 0) for node in nodes if node.get("Index Name")) == 8
        before_blocks = before["Plan"].get("Shared Hit Blocks", 0) + before["Plan"].get(
            "Shared Read Blocks", 0
        )
        after_blocks = after["Plan"].get("Shared Hit Blocks", 0) + after["Plan"].get(
            "Shared Read Blocks", 0
        )
        assert before_blocks > after_blocks
        output = {
            "fixture": {"terminal": 50000, "held": 8, "gross": 3, "workspace_sum": 24},
            "enable_seqscan": "on",
            "before": before,
            "after": after,
            "before_shared_blocks": before_blocks,
            "after_shared_blocks": after_blocks,
        }
        print(json.dumps(output, sort_keys=True))
        raise psycopg.Rollback()
