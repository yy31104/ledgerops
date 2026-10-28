"""Reversal-aware exports and maintenance use all current financial relations."""

from pathlib import Path
from uuid import uuid4

import pytest

from ledgerops.db.maintenance import (
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
from ledgerops.export import capture_snapshot
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import original_rows, reversal_input, reverse

pytestmark = pytest.mark.integration


def test_reversed_history_survives_repair_with_original_and_success_relation_unchanged(
    seeded_db, golden, tmp_path
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    args = reversal_input(golden)
    reversal = reverse(seeded_db, *args)
    assert reversal.http_status == 201
    with seeded_db.connect("reader") as conn:
        assert verify_snapshot(capture_snapshot(conn, golden["workspace"]["id"])).healthy
        relation = conn.execute("SELECT * FROM ledger.purchase_reversal_effect").fetchall()
    # Deliberate SQL-valid projection drift; journals/canonical requests stay intact.
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account_balance SET credits=200 WHERE account_id=%s",
            (golden["purchase_request"]["customer_account_id"],),
        )
    workspace = golden["workspace"]["id"]
    freeze, repair, actor = (str(uuid4()) for _ in range(3))
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, workspace, freeze, actor, "reversal history check")
        preview = preview_rebuild(conn, workspace, freeze, tmp_path, code_revision="0" * 40)
        assert preview["report"]["request_intent_ok"]
        assert not preview["report"]["projection_ok"]
        apply_rebuild(
            conn,
            workspace,
            freeze,
            repair,
            actor,
            "repair projection only",
            Path(preview["export_path"]).name,
        )
        resumed = resume_workspace(conn, workspace, freeze, actor, "checked")
        assert resumed["resume_report"]["healthy"]
        assert resumed["resume_report"]["intent_scope"] == ["PURCHASE", "REVERSAL"]
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT booked FROM ledger.account_balance ORDER BY account_id"
        ).fetchall() == [(100,), (100,), (0,), (0,)]
        assert conn.execute("SELECT * FROM ledger.purchase_reversal_effect").fetchall() == relation
        assert conn.execute("SELECT projection_epoch FROM ledger.workspace").fetchone() == (2,)
        assert verify_snapshot(capture_snapshot(conn, workspace)).healthy
    assert original_rows(seeded_db, original_id) == original
    replay = reverse(seeded_db, *args)
    assert replay.http_status == 201 and replay.replayed and replay.body == reversal.body
