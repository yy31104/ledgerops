"""Private large-source maintenance fixture, supplied over stdin and never installed."""

import json
import os
import re
import resource
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo

from ledgerops.authentication import _workspace_lock
from ledgerops.db.maintenance import (
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
from ledgerops.http.config import bounded_file
from ledgerops.stream_export import export_stream
from ledgerops.stream_verification import verify_stream

os.environ["TMPDIR"] = "/var/lib/ledgerops-scratch"
scratch_before = {path.name for path in Path(os.environ["TMPDIR"]).iterdir()}
state = json.loads(bounded_file(Path("/run/bootstrap/state.json")))
if not Path("/run/bootstrap/acceptance.json").is_file():
    raise ValueError("EXPLICIT_DISPOSABLE_SYNTHETIC_SEED_REQUIRED")
workspace, actor = state["workspace_id"], state["admin_actor"]
provenance = json.loads(Path("/opt/ledgerops-provenance.json").read_bytes())
context = "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1"
freeze_id, repair_id = str(uuid4()), str(uuid4())
reason = "explicit disposable Linux full-source maintenance acceptance"


def connection(role):
    return psycopg.connect(
        make_conninfo(
            host="postgres",
            dbname="ledgerops",
            user=role,
            password=state["passwords"][role],
        ),
        autocommit=True,
    )


def require(condition, code):
    if not condition:
        raise ValueError(code)


def holds(conn):
    return (
        conn.execute(
            "SELECT * FROM ledger.reservation WHERE workspace_id=%s ORDER BY payout_id",
            (workspace,),
        ).fetchall(),
        conn.execute(
            "SELECT * FROM ledger.reservation_event WHERE workspace_id=%s "
            "ORDER BY payout_id,event_no",
            (workspace,),
        ).fetchall(),
    )


def money(conn):
    return conn.execute(
        "SELECT booked,reserved,booked-reserved,version FROM ledger.account_balance "
        "WHERE workspace_id=%s AND account_id=%s",
        (workspace, state["merchant_account"]),
    ).fetchone()


with connection("ledger_evidence_reader") as conn:
    require(
        conn.execute(
            "SELECT count(*) FROM ledger.observation_quarantine WHERE workspace_id=%s", (workspace,)
        ).fetchone()
        == (129,),
        "LARGE_DIAGNOSTIC_FIXTURE_REQUIRED",
    )
    before_holds, before_money = holds(conn), money(conn)
    before_journals = conn.execute(
        "SELECT count(*) FROM ledger.journal WHERE workspace_id=%s", (workspace,)
    ).fetchone()[0]
    require(before_money[:3] == (18, 0, 18), "LITERAL_PRE_MAINTENANCE_BALANCE18")
# Deliberate privileged fault fixture; ordinary service roles never receive this capability.
with connection("postgres") as conn, conn.transaction(), conn.cursor() as cur:
    _workspace_lock(cur, workspace, shared=False)
    cur.execute(
        "UPDATE ledger.account_balance SET credits=credits+1,version=version+1 "
        "WHERE workspace_id=%s AND account_id=%s",
        (workspace, state["merchant_account"]),
    )
with connection("ledger_evidence_reader") as conn:
    require(money(conn)[:3] == (19, 0, 19), "EXPLICIT_BOOKED_DRIFT19")
with connection("ledger_maintenance_login") as conn:
    freeze_workspace(conn, workspace, freeze_id, actor, reason)
    preview = preview_rebuild(
        conn,
        workspace,
        freeze_id,
        "/var/lib/ledgerops-inputs/maintenance-previews",
        code_revision=provenance["revision"],
        dirty=provenance["dirty"],
    )
    preview_path = Path(preview["export_path"])
    manifest = json.loads((preview_path / "manifest.json").read_bytes())
    require(
        manifest["format_version"] == 10 and manifest["total_bytes"] > 16 * 1024 * 1024,
        "ACTUAL_LARGE_STREAM_PREVIEW",
    )
    require(manifest["row_counts"]["observation_quarantines"] == 129, "FULL_PREVIEW_ROW_COUNT")
    require(manifest["source"]["capture_context"] == context, "HONEST_EXCLUSIVE_CAPTURE_CONTEXT")
    report = preview["report"]
    require(
        report["report_version"] == 4
        and report["verification_complete"]
        and report["structure_ok"]
        and report["request_intent_ok"]
        and not report["projection_ok"]
        and report["rebuild_eligible"],
        "FULL_PREVIEW_IDENTIFIES_PROJECTION_DRIFT_ONLY",
    )
    selected = next(
        change for change in preview["changes"] if change["account_id"] == state["merchant_account"]
    )
    require(
        selected["before"]["booked"] == "19"
        and selected["after"]["booked"] == "18"
        and selected["after"]["reserved"] == "0",
        "INDEPENDENT_SQL_REPAIR_TARGET18",
    )
    published = verify_stream(preview_path, scratch_root=os.environ["TMPDIR"])
    require(
        published["verification_complete"]
        and published["structure_ok"]
        and published["request_intent_ok"]
        and not published["projection_ok"],
        "STANDALONE_PREVIEW_VERIFIES_FULL_SOURCE",
    )
    repaired = apply_rebuild(
        conn, workspace, freeze_id, repair_id, actor, reason, preview_path.name
    )
    require((repaired["old_epoch"], repaired["new_epoch"]) == (1, 2), "ONE_ATOMIC_REPAIR_EPOCH")
    require(
        apply_rebuild(conn, workspace, freeze_id, repair_id, actor, reason, preview_path.name)
        == repaired,
        "STABLE_REPAIR_ID_REPLAY",
    )
    resumed = resume_workspace(conn, workspace, freeze_id, actor, reason)
    resume_report = resumed["resume_report"]
    require(
        resume_report["report_version"] == 4
        and resume_report["verification_complete"]
        and resume_report["structure_ok"]
        and resume_report["projection_ok"]
        and resume_report["request_intent_ok"]
        and resume_report["healthy"],
        "COMPLETE_CURRENT_RESUME_A_B",
    )
    require(
        resume_report["capture_context"] == context
        and resume_report["source_export_id"] is None
        and resume_report["source_manifest_sha256"] is None
        and re.fullmatch(r"[0-9a-f]{64}", resume_report["source_content_sha256"]) is not None,
        "PRIVATE_SQL_REPORT_HAS_NO_INVENTED_EXPORT_IDENTITY",
    )
    require(
        resume_workspace(conn, workspace, freeze_id, actor, reason) == resumed,
        "STABLE_RESUME_ID_REPLAY",
    )
with connection("ledger_evidence_reader") as conn:
    after = money(conn)
    require(
        after[:3] == (18, 0, 18) and after[3] == before_money[3] + 2,
        "REPAIRED18_WITH_NO_REPLAY_VERSION_INCREMENT",
    )
    require(holds(conn) == before_holds, "EXACT_HOLD_STATE_AND_HISTORY_UNCHANGED")
    require(
        conn.execute(
            "SELECT count(*) FROM ledger.journal WHERE workspace_id=%s", (workspace,)
        ).fetchone()[0]
        == before_journals,
        "NO_REPAIR_FINANCIAL_JOURNAL",
    )
    require(
        conn.execute(
            "SELECT mode,current_freeze_id,projection_epoch FROM ledger.workspace WHERE id=%s",
            (workspace,),
        ).fetchone()
        == ("NORMAL", None, 2),
        "AUDITED_RESUMED_NORMAL",
    )
    final_path = export_stream(
        conn,
        workspace,
        "/var/lib/ledgerops-inputs/maintenance-after",
        code_revision=provenance["revision"],
        dirty=provenance["dirty"],
    )
final_report = verify_stream(final_path, scratch_root=os.environ["TMPDIR"])
require(
    final_report["healthy"]
    and final_report["verification_complete"]
    and final_report["structure_ok"]
    and final_report["projection_ok"]
    and final_report["request_intent_ok"],
    "PERSISTED_RESUME_V4_FULL_STREAM_VERIFICATION",
)
require(
    {path.name for path in Path(os.environ["TMPDIR"]).iterdir()} == scratch_before,
    "EXACT_OWNED_SCRATCH_CLEANUP",
)
print(
    json.dumps(
        {
            "passed": True,
            "source_format": 10,
            "source_bytes": manifest["total_bytes"],
            "quarantine_rows": 129,
            "freeze_id": freeze_id,
            "repair_id": repair_id,
            "old_epoch": 1,
            "new_epoch": 2,
            "before_booked": "19",
            "after_booked": "18",
            "reserved": "0",
            "hold_history_unchanged": True,
            "journal_count_unchanged": True,
            "stable_identity_replays": True,
            "preview_report_version": 4,
            "resume_report_version": 4,
            "capture_context": context,
            "verification_complete": True,
            "independent_healthy": True,
            "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "cgroup_peak_bytes": int(Path("/sys/fs/cgroup/memory.peak").read_text()),
            "scratch_cleaned": True,
        },
        sort_keys=True,
    )
)
