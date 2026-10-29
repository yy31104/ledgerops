"""Bounded investigation projections inside an already-authorized RR snapshot."""


def operation_case(cur, workspace, operation_id):
    operation = cur.execute(
        "SELECT operation_id,kind,admission,http_status,response_body,created_at,"
        "encode(payload_sha256,'hex') AS payload_sha256 FROM ledger.operation "
        "WHERE workspace_id=%s AND operation_id=%s",
        (workspace, operation_id),
    ).fetchone()
    if operation is None:
        return None
    entries = cur.execute(
        "SELECT j.journal_id,j.kind,j.recorded_at,e.line_no,e.account_id,e.side,e.amount_minor "
        "FROM ledger.journal j JOIN ledger.journal_entry e USING(workspace_id,journal_id) "
        "WHERE j.workspace_id=%s AND j.operation_id=%s ORDER BY j.journal_id,e.line_no LIMIT 101",
        (workspace, operation_id),
    ).fetchall()
    if len(entries) > 100:
        raise ValueError("investigation entry limit")
    for entry in entries:
        entry["amount_minor"] = str(entry["amount_minor"])
    payout = cur.execute(
        "SELECT p.operation_id,p.provider_namespace,p.provider_key,p.knowledge,p.version,"
        "p.principal_minor,p.fee_minor,p.gross_minor,r.state AS hold_state,"
        "r.version AS hold_version "
        "FROM ledger.payout p JOIN ledger.reservation r ON r.workspace_id=p.workspace_id "
        "AND r.payout_id=p.operation_id WHERE p.workspace_id=%s AND p.operation_id=%s",
        (workspace, operation_id),
    ).fetchone()
    if payout is not None:
        for field in ("principal_minor", "fee_minor", "gross_minor", "version", "hold_version"):
            payout[field] = str(payout[field])
    observations = cur.execute(
        "SELECT observation_id,state,source_sequence,received_at,"
        "encode(envelope_sha256,'hex') AS envelope_sha256 FROM ledger.provider_observation "
        "WHERE workspace_id=%s AND payout_id=%s "
        "ORDER BY received_at DESC,observation_id DESC LIMIT 21",
        (workspace, operation_id),
    ).fetchall()
    for observation in observations:
        observation["source_sequence"] = str(observation["source_sequence"])
    incidents = cur.execute(
        "SELECT incident_id,category,code,observation_id,related_observation_id,created_at "
        "FROM ledger.payout_incident WHERE workspace_id=%s AND payout_id=%s "
        "ORDER BY created_at DESC,incident_id DESC LIMIT 33",
        (workspace, operation_id),
    ).fetchall()
    # Registry references are navigation aids, not verification of object bytes.
    current = cur.execute(
        "SELECT c.run_id,c.provider_namespace,c.business_date,c.evidence_epoch,"
        "r.result_sha256,g.evidence_epoch AS workspace_evidence_epoch "
        "FROM ledger.recon_current c JOIN ledger.recon_run r USING(workspace_id,run_id) "
        "JOIN ledger.recon_registry_guard g USING(workspace_id) "
        "WHERE c.workspace_id=%s AND c.provider_namespace=%s "
        "ORDER BY c.business_date DESC LIMIT 21",
        (workspace, None if payout is None else payout["provider_namespace"]),
    ).fetchall()
    for reference in current:
        reference["business_date"] = reference["business_date"].isoformat()
        for field in ("evidence_epoch", "workspace_evidence_epoch"):
            reference[field] = str(reference[field])
    return {
        "operation": operation,
        "entries": entries,
        "payout": payout,
        "recent_observations": observations[:20],
        "observations_have_more": len(observations) > 20,
        "recent_incidents": incidents[:32],
        "incidents_have_more": len(incidents) > 32,
        "current_reconciliation_references": current[:20],
        "reconciliations_have_more": len(current) > 20,
        "limits": {"entries": 100, "observations": 20, "incidents": 32},
        "evidence_boundary": (
            "Stored hashes and references; no provider authenticity or whole-history assertion"
        ),
    }


def metrics(cur, workspace):
    row = cur.execute(
        "SELECT count(*) AS payout_count,"
        "count(*) FILTER(WHERE p.knowledge IN ('UNKNOWN','ACCEPTED','NOT_SENT')) "
        "AS unresolved_count,"
        "coalesce(max(extract(epoch FROM clock_timestamp()-o.created_at)) "
        "FILTER(WHERE p.knowledge IN ('UNKNOWN','ACCEPTED','NOT_SENT')),0) "
        "AS oldest_unresolved_seconds "
        "FROM ledger.payout p JOIN ledger.operation o USING(workspace_id,operation_id) "
        "WHERE p.workspace_id=%s",
        (workspace,),
    ).fetchone()
    row["blocked_count"] = cur.execute(
        "SELECT count(*) AS count FROM ledger.dispatch_intent "
        "WHERE workspace_id=%s AND state='BLOCKED'",
        (workspace,),
    ).fetchone()["count"]
    row["quarantine_count"] = cur.execute(
        "SELECT count(*) AS count FROM ledger.observation_quarantine WHERE workspace_id=%s",
        (workspace,),
    ).fetchone()["count"]
    return row


def operation_receipt(cur, workspace, client, operation_id):
    return cur.execute(
        "SELECT o.operation_id,o.kind,o.admission,o.http_status,o.response_body,o.created_at "
        "FROM ledger.operation o WHERE o.workspace_id=%s AND o.operation_id=%s "
        "AND EXISTS(SELECT 1 FROM ledger.request_result r WHERE r.workspace_id=o.workspace_id "
        "AND r.operation_id=o.operation_id AND r.client_id=%s)",
        (workspace, operation_id, client),
    ).fetchone()
