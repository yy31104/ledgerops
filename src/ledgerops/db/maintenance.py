"""Cooperating writer drain and atomic projection repair, under explicit SQL locks.

The caller authenticates the maintenance actor and supplies a dedicated maintenance
connection. Advisory barriers do not constrain raw owner SQL or stop external sends.
Preview files are evidence: application computes all target balances from live entries.
"""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from uuid import UUID

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledgerops.maintenance_snapshot import assess_current

_MAX_VERSION = 2**63 - 1
_MAX_TOTAL = 10**38 - 1


class MaintenanceError(ValueError):
    """A maintenance precondition failed; no change from this call commits."""

    def __init__(self, code, *, report=None):
        self.code, self.report = code, report
        super().__init__(code)


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise MaintenanceError("INVALID_INPUT") from exc
    return value


def _reason(value):
    try:
        valid = type(value) is str and "\x00" not in value and 1 <= len(value.encode()) <= 256
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise MaintenanceError("INVALID_INPUT")
    return value


def _metadata(row):
    return {
        key: (
            str(value)
            if isinstance(value, UUID)
            else value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if isinstance(value, datetime)
            else value
        )
        for key, value in row.items()
    }


@contextmanager
def _transaction(conn, workspace_id):
    _uuid(workspace_id)
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise MaintenanceError("IDLE_AUTOCOMMIT_REQUIRED")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
        cur.execute("SELECT set_config('lock_timeout', '5s', true)")
        cur.execute("SELECT set_config('statement_timeout', '10s', true)")
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"ledgerops:workspace:{workspace_id}",),
        )
        workspace = cur.execute(
            "SELECT * FROM ledger.workspace WHERE id=%s", (workspace_id,)
        ).fetchone()
        if workspace is None:
            raise MaintenanceError("NOT_FOUND")
        yield cur, workspace


def _current_freeze(workspace, freeze_id):
    if workspace["mode"] == "RESTORE":
        raise MaintenanceError("RESTORE_UNSUPPORTED")
    if workspace["mode"] != "FROZEN":
        raise MaintenanceError("FROZEN_REQUIRED")
    if str(workspace["current_freeze_id"]) != freeze_id:
        raise MaintenanceError("FREEZE_CONFLICT")


def freeze_workspace(conn, workspace_id, freeze_id, actor_id, reason):
    """Wait for cooperating writers, then durably freeze; same identity never refreezes."""
    _uuid(freeze_id), _uuid(actor_id), _reason(reason)
    with _transaction(conn, workspace_id) as (cur, workspace):
        retained = cur.execute(
            "SELECT * FROM ledger.maintenance_freeze WHERE workspace_id=%s AND freeze_id=%s",
            (workspace_id, freeze_id),
        ).fetchone()
        if retained is not None:
            if str(retained["actor_id"]) != actor_id or retained["reason"] != reason:
                raise MaintenanceError("IDENTITY_CONFLICT")
            result = _metadata(retained)
        else:
            if workspace["mode"] == "RESTORE":
                raise MaintenanceError("RESTORE_UNSUPPORTED")
            if workspace["mode"] != "NORMAL" or workspace["current_freeze_id"] is not None:
                raise MaintenanceError("FREEZE_CONFLICT")
            retained = cur.execute(
                """INSERT INTO ledger.maintenance_freeze
                   (workspace_id,freeze_id,actor_id,reason) VALUES (%s,%s,%s,%s) RETURNING *""",
                (workspace_id, freeze_id, actor_id, reason),
            ).fetchone()
            cur.execute(
                "UPDATE ledger.workspace SET mode='FROZEN',current_freeze_id=%s WHERE id=%s",
                (freeze_id, workspace_id),
            )
            result = _metadata(retained)
    return result


def _targets(cur, workspace_id, snapshot):
    """SQL aggregation independent of the verifier's Python fold; include idle accounts."""
    # Aggregate the two histories separately: a join between raw journal lines
    # and hold events would multiply both totals for accounts with several rows.
    hold_cte = "held AS (SELECT NULL::uuid AS account_id,0::numeric AS reserved WHERE false)"
    if snapshot["format_version"] >= 5:
        hold_cte = """latest_hold AS (
            SELECT DISTINCT ON (e.payout_id) e.payout_id,e.state
            FROM ledger.reservation_event e WHERE e.workspace_id=%s
            ORDER BY e.payout_id,e.event_no DESC
        ), held AS (
            SELECT r.account_id,sum(r.gross_minor) AS reserved
            FROM latest_hold e JOIN ledger.reservation r ON r.payout_id=e.payout_id
            WHERE r.workspace_id=%s AND e.state='HELD' GROUP BY r.account_id
        )"""
    params = (
        (workspace_id, workspace_id, workspace_id, workspace_id)
        if snapshot["format_version"] >= 5
        else (workspace_id, workspace_id)
    )
    rows = cur.execute(
        "WITH "
        + hold_cte
        + """, posted AS (
           SELECT account_id,
           coalesce(sum(e.amount_minor) FILTER (WHERE e.side='D'),0) AS debits,
           coalesce(sum(e.amount_minor) FILTER (WHERE e.side='C'),0) AS credits
           FROM ledger.journal_entry e WHERE e.workspace_id=%s GROUP BY account_id
        ) SELECT a.id,a.normal_side,coalesce(p.debits,0) AS debits,
           coalesce(p.credits,0) AS credits,coalesce(h.reserved,0) AS reserved
           FROM ledger.account a LEFT JOIN posted p ON p.account_id=a.id
           LEFT JOIN held h ON h.account_id=a.id
           WHERE a.workspace_id=%s ORDER BY a.id""",
        params,
    ).fetchall()
    before = {row["account_id"]: row for row in snapshot["balances"]}
    changes = []
    for row in rows:
        identifier = str(row["id"])
        old = before.get(identifier)
        version = 0 if old is None else old["version"]
        debit, credit = int(row["debits"]), int(row["credits"])
        reserved = int(row["reserved"])
        booked = debit - credit if row["normal_side"] == "D" else credit - debit
        if (
            version >= _MAX_VERSION
            or min(debit, credit, booked) < 0
            or max(debit, credit, booked) > _MAX_TOTAL
            or reserved < 0
            or booked < reserved
        ):
            raise MaintenanceError("LIMIT_EXCEEDED")
        old_fields = ("debits", "credits", "reserved", "booked", "version")
        changes.append(
            {
                "account_id": identifier,
                "normal_side": row["normal_side"],
                "before": None if old is None else {key: old[key] for key in old_fields},
                "after": {
                    "debits": str(debit),
                    "credits": str(credit),
                    "reserved": str(reserved),
                    "booked": str(booked),
                    "version": version + 1,
                },
            }
        )
    return changes


def preview_rebuild(conn, workspace_id, freeze_id, output_root, *, code_revision, dirty=False):
    """Capture frozen evidence/differences; publication is outside the DB transaction."""
    _uuid(freeze_id)
    with ExitStack() as resources:
        with _transaction(conn, workspace_id) as (cur, workspace):
            _current_freeze(workspace, freeze_id)
            assessment = resources.enter_context(assess_current(cur, workspace_id))
            changes = (
                _targets(cur, workspace_id, assessment.projection_input())
                if assessment.report.rebuild_eligible
                else []
            )
        destination = assessment.publish(output_root, code_revision=code_revision, dirty=dirty)
        return {
            "export_path": str(destination),
            "report": assessment.report.as_dict(),
            "changes": changes,
        }


def _require_source(report, *, projected=False):
    if getattr(report, "verification_complete", True) is not True:
        raise MaintenanceError("VERIFICATION_INCOMPLETE", report=report.as_dict())
    if not report.structure_ok or not report.request_intent_ok:
        raise MaintenanceError("INVALID_SOURCE", report=report.as_dict())
    if not report.reservation_supported:
        raise MaintenanceError("RESERVED_UNSUPPORTED", report=report.as_dict())
    if projected and not report.projection_ok:
        raise MaintenanceError("PROJECTION_DRIFT", report=report.as_dict())


def _write_projection(cur, workspace_id, repair_id, change, *, include_reserved=True):
    before, after = change["before"], change["after"]
    common = (workspace_id, change["account_id"])
    values = (after["debits"], after["credits"])
    if include_reserved:
        values += (after["reserved"],)
    values += (after["version"],)
    if before is None:
        cur.execute(
            "INSERT INTO ledger.account_balance (workspace_id,account_id,normal_side,"
            "debits,credits," + ("reserved," if include_reserved else "") + "version) "
            "VALUES (%s,%s,%s," + ",".join("%s" for _ in values) + ")",
            (*common, change["normal_side"], *values),
        )
    else:
        cur.execute(
            "UPDATE ledger.account_balance SET debits=%s,credits=%s,"
            + ("reserved=%s," if include_reserved else "")
            + "version=%s WHERE workspace_id=%s AND account_id=%s",
            (*values, *common),
        )
    fields = ("debits", "credits", "reserved", "booked", "version")
    cur.execute(
        """INSERT INTO ledger.projection_repair_row
           (workspace_id,repair_id,account_id,normal_side,
            before_debits,before_credits,before_reserved,before_booked,before_version,
            after_debits,after_credits,after_reserved,after_booked,after_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            workspace_id,
            repair_id,
            change["account_id"],
            change["normal_side"],
            *(None if before is None else before[key] for key in fields),
            *(after[key] for key in fields),
        ),
    )


def apply_rebuild(conn, workspace_id, freeze_id, repair_id, actor_id, reason, preview_export_id):
    """Freshly recompute/apply/audit/advance epoch atomically; same ID resolves commit loss.

    preview_export_id is an audit reference supplied by the caller, never a source
    of target balances or a claim that this process authenticated a preview file.
    """
    for value in (freeze_id, repair_id, actor_id, preview_export_id):
        _uuid(value)
    _reason(reason)
    with _transaction(conn, workspace_id) as (cur, workspace):
        retained = cur.execute(
            "SELECT * FROM ledger.projection_repair WHERE workspace_id=%s AND repair_id=%s",
            (workspace_id, repair_id),
        ).fetchone()
        identity = dict(
            freeze_id=freeze_id,
            actor_id=actor_id,
            reason=reason,
            preview_export_id=preview_export_id,
        )
        if retained is not None:
            result = _metadata(retained)
            if any(result[key] != value for key, value in identity.items()):
                raise MaintenanceError("IDENTITY_CONFLICT")
        else:
            _current_freeze(workspace, freeze_id)
            with assess_current(cur, workspace_id) as assessment:
                _require_source(assessment.report)
                snapshot = assessment.projection_input()
            epoch = workspace["projection_epoch"]
            if epoch >= _MAX_VERSION:
                raise MaintenanceError("LIMIT_EXCEEDED")
            changes = _targets(cur, workspace_id, snapshot)
            hold_format = snapshot["format_version"] >= 5
            retained = cur.execute(
                """INSERT INTO ledger.projection_repair
                   (workspace_id,repair_id,freeze_id,actor_id,reason,preview_export_id,
                    source_witness,old_epoch,new_epoch,account_count"""
                + (",fold_version" if hold_format else "")
                + ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s"
                + (",%s" if hold_format else "")
                + ") RETURNING *",
                (
                    workspace_id,
                    repair_id,
                    freeze_id,
                    actor_id,
                    reason,
                    preview_export_id,
                    snapshot["snapshot_witness"],
                    epoch,
                    epoch + 1,
                    len(changes),
                )
                + ((2,) if hold_format else ()),
            ).fetchone()
            for change in changes:
                if hold_format:
                    _write_projection(cur, workspace_id, repair_id, change)
                else:
                    _write_projection(cur, workspace_id, repair_id, change, include_reserved=False)
            cur.execute(
                "UPDATE ledger.workspace SET projection_epoch=%s WHERE id=%s",
                (epoch + 1, workspace_id),
            )
            with assess_current(cur, workspace_id) as assessment:
                _require_source(assessment.report, projected=True)
            result = _metadata(retained)
    return result


def resume_workspace(conn, workspace_id, freeze_id, actor_id, reason):
    """Require structure/A; retain B/reservation findings without using repair's gate."""
    _uuid(freeze_id), _uuid(actor_id), _reason(reason)
    with _transaction(conn, workspace_id) as (cur, workspace):
        retained = cur.execute(
            "SELECT * FROM ledger.maintenance_freeze WHERE workspace_id=%s AND freeze_id=%s",
            (workspace_id, freeze_id),
        ).fetchone()
        if retained is None:
            raise MaintenanceError("FREEZE_CONFLICT")
        if retained["resumed_at"] is not None:
            if str(retained["resumed_by"]) != actor_id or retained["resume_reason"] != reason:
                raise MaintenanceError("IDENTITY_CONFLICT")
            result = _metadata(retained)
        else:
            _current_freeze(workspace, freeze_id)
            with assess_current(cur, workspace_id) as assessment:
                report = assessment.report
            if getattr(report, "verification_complete", True) is not True:
                raise MaintenanceError("VERIFICATION_INCOMPLETE", report=report.as_dict())
            if not report.structure_ok:
                raise MaintenanceError("INVALID_SOURCE", report=report.as_dict())
            if not report.projection_ok:
                raise MaintenanceError("PROJECTION_DRIFT", report=report.as_dict())
            retained = cur.execute(
                """UPDATE ledger.maintenance_freeze
                   SET resumed_at=clock_timestamp(),resumed_by=%s,resume_reason=%s,resume_report=%s
                   WHERE workspace_id=%s AND freeze_id=%s RETURNING *""",
                (actor_id, reason, Jsonb(report.as_dict()), workspace_id, freeze_id),
            ).fetchone()
            cur.execute(
                "UPDATE ledger.workspace SET mode='NORMAL',current_freeze_id=NULL WHERE id=%s",
                (workspace_id,),
            )
            result = _metadata(retained)
    return result
