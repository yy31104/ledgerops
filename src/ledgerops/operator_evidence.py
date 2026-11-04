"""Pinned current reconciliation evidence for the one typed operator action.

The caller owns the workspace barrier and transaction. Registry locking precedes
its auth and provider locks; this helper never opens a nested transaction or
releases the caller's locks. Permanent RUN pins retain source bytes after return.
"""

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from ledgerops.export import MAX_ROWS_PER_TABLE, ExportError, _normal_row, _validate_snapshot
from ledgerops.reconciliation import semantic_digest
from ledgerops.reconciliation.publication import PublicationError, _pin, _registry, _strict_json
from ledgerops.verification import verify_snapshot

MAX_ACTION_EVIDENCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ActionEvidence:
    evidence_epoch: int
    result_sha256: str
    ledger_data_sha256: str
    snapshot: dict
    result: dict


def _require(condition, code):
    if not condition:
        raise PublicationError(code)


def load_action_evidence(cur, workspace_id, provider_namespace, run_id, store):
    """Verify exactly the requested current COMPLETE run and every retained object.

    Reader pins live only within this registry-locked transaction and are explicitly
    removed after the bytes are read. Existing durable RUN pins remain untouched.
    Missing/corrupt objects fail; a different run is never substituted.
    """
    workspace, namespace, run_id = (
        UUID(str(workspace_id)),
        UUID(str(provider_namespace)),
        UUID(str(run_id)),
    )
    epoch = _registry(cur, workspace)
    run = cur.execute(
        "SELECT * FROM ledger.recon_run WHERE workspace_id=%s AND run_id=%s",
        (workspace, run_id),
    ).fetchone()
    _require(run is not None and run["state"] == "COMPLETE", "RUN_NOT_COMPLETE")
    _require(run["provider_namespace"] == namespace, "EVIDENCE_NAMESPACE_MISMATCH")
    current = cur.execute(
        "SELECT run_id,evidence_epoch FROM ledger.recon_current WHERE workspace_id=%s "
        "AND provider_namespace=%s AND business_date=%s",
        (workspace, namespace, run["business_date"]),
    ).fetchone()
    _require(
        current is not None
        and current["run_id"] == run_id
        and run["published"]
        and current["evidence_epoch"] == run["published_epoch"],
        "STALE_EVIDENCE_RUN",
    )
    _require(
        hashlib.sha256(run["manifest"]).hexdigest() == run["manifest_sha256"]
        and hashlib.sha256(run["partition_plan"]).hexdigest() == run["plan_sha256"],
        "CORRUPT_RUN_METADATA",
    )
    manifest = _strict_json(run["manifest"])
    _require(type(manifest) is dict, "INVALID_INPUT_MANIFEST")
    kind, version = manifest.get("source_kind"), manifest.get("manifest_version")
    _require(kind in (None, "FINANCIAL_STREAM10_COMPACT_V1"), "UNSUPPORTED_EVIDENCE_SOURCE_KIND")
    _require(
        type(version) is int and version == (1 if kind is None else 2),
        "UNSUPPORTED_EVIDENCE_SOURCE_VERSION",
    )
    _require(
        manifest.get("workspace_id") == str(workspace)
        and manifest.get("provider_namespace") == str(namespace)
        and manifest.get("business_date") == run["business_date"].isoformat(),
        "EVIDENCE_MANIFEST_MISMATCH",
    )
    references = cur.execute(
        "SELECT p.object_sha256,o.byte_length FROM ledger.evidence_pin p "
        "JOIN ledger.recon_object o ON o.workspace_id=p.workspace_id AND o.sha256=p.object_sha256 "
        "WHERE p.workspace_id=%s AND p.owner_type='RUN' AND p.run_id=%s ORDER BY p.object_sha256",
        (workspace, run_id),
    ).fetchall()
    is_stream = manifest.get("source_kind") == "FINANCIAL_STREAM10_COMPACT_V1"
    reference_map = {row["object_sha256"]: row["byte_length"] for row in references}
    small = {run["result_sha256"]}
    if is_stream:
        try:
            small.update(
                manifest["inputs"][name]["sha256"]
                for name in ("ledger_data", "ledger_manifest", "statement")
            )
        except (KeyError, TypeError) as exc:
            raise PublicationError("INVALID_INPUT_MANIFEST") from exc
    _require(
        sum(reference["byte_length"] for reference in references)
        <= (512 * 1024 * 1024 if is_stream else MAX_ACTION_EVIDENCE_BYTES),
        "ACTION_EVIDENCE_CAPACITY",
    )
    reader = uuid4()
    objects = {}
    try:
        for reference in references:
            digest = reference["object_sha256"]
            _pin(cur, workspace, digest, "READER", reader, run_id)
            raw = store.read(workspace, digest, reference["byte_length"])
            if not is_stream or digest in small:
                objects[digest] = raw
    finally:
        cur.execute(
            "DELETE FROM ledger.evidence_pin WHERE workspace_id=%s AND owner_type='READER' "
            "AND owner_id=%s",
            (workspace, reader),
        )
    accepted = cur.execute(
        "SELECT result_sha256 FROM ledger.recon_accepted_result "
        "WHERE workspace_id=%s AND run_id=%s",
        (workspace, run_id),
    ).fetchall()
    _require(
        len(accepted) == run["partition_count"]
        and all(row["result_sha256"] in reference_map for row in accepted),
        "RUN_REFERENCE_MISSING",
    )
    _require(run["result_sha256"] in objects, "RUN_REFERENCE_MISSING")
    artifact = _strict_json(objects[run["result_sha256"]])
    _require(type(artifact) is dict, "CORRUPT_RUN_ARTIFACT")
    result = artifact.get("result")
    _require(
        artifact.get("run_id") == str(run_id)
        and artifact.get("input_manifest_sha256") == run["manifest_sha256"]
        and artifact.get("partition_plan_sha256") == run["plan_sha256"]
        and artifact.get("semantic_sha256") == run["semantic_sha256"]
        and semantic_digest(result) == run["semantic_sha256"],
        "CORRUPT_RUN_ARTIFACT",
    )
    _require(
        type(result) is dict
        and result.get("status") == "COMPLETE"
        and result.get("usable") is True
        and result.get("inputs") == manifest,
        "UNUSABLE_EVIDENCE_RUN",
    )
    inputs = manifest.get("inputs")
    _require(
        type(inputs) is dict
        and (
            {"ledger_data", "ledger_manifest", "statement"} <= set(inputs)
            if is_stream
            else set(inputs) == {"ledger_data", "ledger_manifest", "statement"}
        ),
        "INVALID_INPUT_MANIFEST",
    )
    for reference in inputs.values():
        _require(
            type(reference) is dict
            and reference.get("sha256") in reference_map
            and reference.get("bytes") == reference_map[reference["sha256"]],
            "RUN_REFERENCE_MISSING",
        )
    data_digest = inputs["ledger_data"]["sha256"]
    if is_stream:
        from ledgerops.stream_reconciliation import (
            internal_report,
            load_pinned_compact,
            target_snapshot,
        )

        try:
            compact = load_pinned_compact(
                objects[data_digest],
                objects[inputs["ledger_manifest"]["sha256"]],
                lambda chunk: _pinned_stream_chunk(store, workspace, reference_map, chunk),
            )
            expected = [
                {"name": f"stream_chunk_{ordinal:06d}", **chunk}
                for ordinal, chunk in enumerate(compact["chunks"])
            ]
            _require(manifest.get("source_chunks") == expected, "STREAM_INPUT_COVERAGE_MISMATCH")
            snapshot = target_snapshot(compact)
            report = internal_report(compact)
            _require(
                report["verification_complete"]
                and report["structure_ok"]
                and report["projection_ok"]
                and report["request_intent_ok"],
                "UNSAFE_EVIDENCE_SOURCE",
            )
        except PublicationError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise PublicationError("INVALID_STREAM_EVIDENCE_SOURCE") from exc
    else:
        snapshot = _strict_json(objects[data_digest])
        try:
            _validate_snapshot(snapshot)
        except ExportError as exc:
            raise PublicationError("INVALID_EVIDENCE_SOURCE") from exc
        checked = verify_snapshot(snapshot)
        _require(
            checked.structure_ok and checked.projection_ok and checked.request_intent_ok,
            "UNSAFE_EVIDENCE_SOURCE",
        )
        report = checked.as_dict()
    _require(snapshot["workspace"]["id"] == str(workspace), "EVIDENCE_WORKSPACE_MISMATCH")
    _require(result.get("internal_verification") == report, "EVIDENCE_SOURCE_REPORT_MISMATCH")
    return ActionEvidence(epoch, run["result_sha256"], data_digest, snapshot, result)


def evidence_target(snapshot, payout_id):
    """Lossless finite target facts; signed observation bytes are bound by digest."""
    payout_id = str(payout_id)
    payouts = [row for row in snapshot["payouts"] if row["operation_id"] == payout_id]
    holds = [row for row in snapshot["reservations"] if row["payout_id"] == payout_id]
    _require(len(payouts) == len(holds) == 1, "EVIDENCE_TARGET_MISSING")
    payout = {key: value for key, value in payouts[0].items() if key != "provider_request_hex"}
    ids = {payout["merchant_account_id"]}
    clearing = [row["id"] for row in snapshot["accounts"] if row["class"] == "SYNTHETIC_CLEARING"]
    _require(len(clearing) == 1, "EVIDENCE_TARGET_INVALID")
    ids.update(clearing)
    accounts = sorted(
        (row for row in snapshot["accounts"] if row["id"] in ids), key=lambda row: row["id"]
    )
    balances = sorted(
        (row for row in snapshot["balances"] if row["account_id"] in ids),
        key=lambda row: row["account_id"],
    )
    _require(len(accounts) == len(balances) == len(ids), "EVIDENCE_TARGET_MISSING")
    return {
        "projection_epoch": snapshot["workspace"]["projection_epoch"],
        "payout": payout,
        "reservation": holds[0],
        "accounts": accounts,
        "balances": balances,
        "observations": sorted(
            (
                {
                    "observation_id": row["observation_id"],
                    "envelope_sha256": row["envelope_sha256"],
                }
                for row in snapshot["provider_observations"]
                if row["payout_id"] == payout_id
            ),
            key=lambda row: row["observation_id"],
        ),
        "incidents": sorted(
            (row for row in snapshot["payout_incidents"] if row["payout_id"] == payout_id),
            key=lambda row: row["incident_id"],
        ),
        "related_incidents": sorted(
            (row for row in snapshot["related_incidents"] if row["payout_id"] == payout_id),
            key=lambda row: (row["incident_id"], row["observation_id"]),
        ),
    }


def assert_target_current(cur, evidence, payout_id):
    """Lock the hold and balances, then reject source/current target differences."""
    source = evidence_target(evidence.snapshot, payout_id)
    workspace = evidence.snapshot["workspace"]["id"]
    payout_id = str(payout_id)
    cur.execute(
        "SELECT 1 FROM ledger.reservation WHERE workspace_id=%s AND payout_id=%s FOR UPDATE",
        (workspace, payout_id),
    )
    for row in source["balances"]:
        cur.execute(
            "SELECT 1 FROM ledger.account_balance "
            "WHERE workspace_id=%s AND account_id=%s FOR UPDATE",
            (workspace, row["account_id"]),
        )
    # Explicit allowlist mirrors financial exports; no caller-selected SQL identifiers.
    from ledgerops.export import _TABLES_BY_FORMAT

    current = {
        "workspace": cur.execute(
            "SELECT projection_epoch FROM ledger.workspace WHERE id=%s", (workspace,)
        ).fetchone()
    }
    for name in (
        "payouts",
        "reservations",
        "accounts",
        "balances",
        "provider_observations",
        "payout_incidents",
        "related_incidents",
    ):
        table, fields, ordering = _TABLES_BY_FORMAT[evidence.snapshot["format_version"]][name]
        if name in {"accounts", "balances"}:
            column = "id" if name == "accounts" else "account_id"
            clause, values = (
                f" AND {column}=ANY(%s::uuid[])",
                [row["account_id"] for row in source["balances"]],
            )
        else:
            column = "operation_id" if name == "payouts" else "payout_id"
            clause, values = f" AND {column}=%s", payout_id
        rows = cur.execute(
            f"SELECT {fields} FROM ledger.{table} WHERE workspace_id=%s{clause} "
            f"ORDER BY {ordering} LIMIT %s",
            (workspace, values, MAX_ROWS_PER_TABLE + 1),
        ).fetchall()
        _require(len(rows) <= MAX_ROWS_PER_TABLE, "EVIDENCE_TARGET_CAPACITY")
        current[name] = [_normal_row(row) for row in rows]
    target = evidence_target(current, payout_id)
    _require(target == source, "STALE_EVIDENCE_TARGET")
    bad = {str(payout_id), source["payout"]["provider_key"]}
    _require(
        not any(
            row["object_id"] in bad and row["category"] not in {"MISSING_INTERNAL", "TIMING_ONLY"}
            for row in evidence.result["issues"]
        ),
        "UNSAFE_EVIDENCE_TARGET",
    )
    unresolved_success = not any(
        row["payout_id"] == payout_id for row in evidence.snapshot["payout_resolutions"]
    ) and any(
        row["payout_id"] == payout_id and row["kind"] == "execution" and row["state"] == "SUCCEEDED"
        for row in evidence.snapshot["provider_observations"]
    )
    if unresolved_success:
        # A current run can still pin a provider inventory from before the
        # retained success. Require that inventory to corroborate this effect.
        _require(
            any(
                row["object_id"] in bad and row["category"] == "MISSING_INTERNAL"
                for row in evidence.result["issues"]
            ),
            "EVIDENCE_PROVIDER_EFFECT_MISSING",
        )
    return source


def _pinned_stream_chunk(store, workspace, references, chunk):
    _require(references.get(chunk["sha256"]) == chunk["bytes"], "RUN_REFERENCE_MISSING")
    return store.read(workspace, chunk["sha256"], chunk["bytes"])
