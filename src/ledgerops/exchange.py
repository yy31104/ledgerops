"""Offline selected synthetic evidence exchange; no database or operational callback.

The input is one completed financial export and optional historical pinned R4 read
files. Selection is explicit. Original export bytes are read and never rewritten.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from ledgerops.exchange_consumer import (
    COMMIT_TIME,
    EXTERNAL_TIME,
    MAX_BYTES,
    SORT_FIELDS,
    TABLE_FIELDS,
    canonical,
    decode,
    digest,
    fields,
    load_exchange,
    require,
    uuid,
    validate,
)
from ledgerops.export import LoadedExport, load_export_bundle

_MAX_PIN_BYTES = 64 * 1024 * 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _compact(value):
    return canonical(value)[:-1]


def _row(row, names, **changes):
    value = {name: row[name] for name in names.split() if name not in changes}
    value.update(changes)
    for name in ("version", "contract_version", "source_sequence", "line_count", "line_no"):
        if name in value and value[name] is not None:
            value[name] = str(value[name])
    return value


def _reconciliation(raw, source):
    "Verify the complete pinned-read artifact before retaining safe references."
    require(type(raw) is bytes and 0 < len(raw) <= _MAX_PIN_BYTES, "PINNED_READ_BYTE_LIMIT")
    pinned = decode(raw)
    fields(
        pinned,
        (
            "workspace_id run_id reader_id result_sha256 semantic_sha256 "
            "manifest partition_plan result published_epoch is_current_at_pin "
            "evidence_epoch_at_pin"
        ),
    )
    require(pinned["workspace_id"] == source["workspace_id"], "RECONCILIATION_WORKSPACE_MISMATCH")
    manifest, result = pinned["manifest"], pinned["result"]
    require(
        type(manifest) is dict
        and type(result) is dict
        and result.get("status") == "COMPLETE"
        and result.get("usable") is True,
        "UNUSABLE_PINNED_READ",
    )
    require(result.get("inputs") == manifest, "RECONCILIATION_SOURCE_MISMATCH")
    stream_source = source.get("source_kind") == "FINANCIAL_STREAM10"
    require(
        type(manifest.get("manifest_version")) is int
        and manifest["manifest_version"] == (2 if stream_source else 1)
        and (
            manifest.get("source_kind") == "FINANCIAL_STREAM10_COMPACT_V1"
            if stream_source
            else "source_kind" not in manifest
        ),
        "UNSUPPORTED_RECONCILIATION_SOURCE_VERSION",
    )
    require(
        manifest.get("workspace_id") == source["workspace_id"]
        and result.get("workspace_id") == source["workspace_id"]
        and all(
            result.get(name) == manifest.get(name)
            for name in (
                "provider_namespace",
                "business_date",
                "timezone",
                "statement_version",
                "rule_version",
                "parser_version",
            )
        )
        and result.get("window")
        == {
            "start_inclusive": manifest.get("start_inclusive"),
            "end_exclusive": manifest.get("end_exclusive"),
        },
        "RECONCILIATION_SCOPE_MISMATCH",
    )
    require(
        manifest.get("export_id") == source["export_id"]
        and manifest.get("ledger_source_code_revision") == source["code_revision"]
        and manifest.get("ledger_source_dirty") is source["dirty"]
        and manifest["inputs"]["ledger_manifest"]["sha256"] == source["manifest_sha256"],
        "RECONCILIATION_SOURCE_MISMATCH",
    )
    if stream_source:
        internal = result.get("internal_verification")
        require(
            manifest.get("manifest_version") == 2
            and manifest.get("source_kind") == "FINANCIAL_STREAM10_COMPACT_V1"
            and manifest["inputs"]["ledger_manifest"]["sha256"] == source["manifest_sha256"]
            and type(internal) is dict
            and internal.get("source_manifest_sha256") == source["manifest_sha256"]
            and internal.get("source_content_sha256") == source["data_sha256"],
            "RECONCILIATION_STREAM_SOURCE_MISMATCH",
        )
    else:
        require(
            manifest["inputs"]["ledger_data"]["sha256"] == source["data_sha256"],
            "RECONCILIATION_SOURCE_MISMATCH",
        )
    manifest_sha = _sha(_compact(manifest))
    plan_sha = _sha(_compact(pinned["partition_plan"]))
    require(
        _sha(_compact(result)) == pinned["semantic_sha256"], "RECONCILIATION_SEMANTIC_HASH_MISMATCH"
    )
    artifact = {
        "run_id": pinned["run_id"],
        "input_manifest_sha256": manifest_sha,
        "partition_plan_sha256": plan_sha,
        "semantic_sha256": pinned["semantic_sha256"],
        "result": result,
    }
    require(
        _sha(_compact(artifact)) == digest(pinned["result_sha256"]),
        "RECONCILIATION_RESULT_HASH_MISMATCH",
    )
    reference = {
        "run_id": pinned["run_id"],
        "provider_namespace": manifest["provider_namespace"],
        "business_date": manifest["business_date"],
        "timezone": manifest["timezone"],
        "start_inclusive": manifest["start_inclusive"],
        "end_exclusive": manifest["end_exclusive"],
        "effect_watermark": str(manifest["effect_watermark"]),
        "assignment_watermark": str(manifest["assignment_watermark"]),
        "statement_version": str(manifest["statement_version"]),
        "result_sha256": pinned["result_sha256"],
        "semantic_sha256": pinned["semantic_sha256"],
        "input_manifest_sha256": manifest_sha,
        "partition_plan_sha256": plan_sha,
        "ledger_data_sha256": source["data_sha256"],
        "statement_sha256": manifest["inputs"]["statement"]["sha256"],
        "evidence_epoch_at_pin": str(pinned["evidence_epoch_at_pin"]),
        "published_epoch": None
        if pinned["published_epoch"] is None
        else str(pinned["published_epoch"]),
        "is_current_at_pin": pinned["is_current_at_pin"],
        "reader_pin_id": pinned["reader_id"],
        "pinned_read_sha256": _sha(raw),
        "reference_status": "HISTORICAL_PIN_REFERENCE_NOT_LIVE_AUTHORITY",
    }
    if stream_source:
        reference.update(
            reference_status="HISTORICAL_STREAM_PIN_REFERENCE_NOT_LIVE_AUTHORITY",
            matcher_data_sha256=manifest["inputs"]["ledger_data"]["sha256"],
            full_stream_manifest_sha256=source["manifest_sha256"],
        )
    return reference


def build_exchange(bundle, *, payout_ids=(), journal_ids=(), pinned_reads=()):
    """Return deterministic v1 bytes from exact source bytes and an explicit scope.

    No timestamp filter is applied: snapshot membership is the cutoff. recorded_at,
    received_at and resolved_at remain local record facts, never commit or external
    execution times. Supporting account closure includes only referenced accounts.
    """
    require(type(bundle) is LoadedExport, "COMPLETED_FINANCIAL_EXPORT_REQUIRED")
    snapshot, source_manifest = bundle.snapshot, bundle.manifest
    source = {
        "workspace_id": snapshot["workspace"]["id"],
        "export_id": source_manifest["export_id"],
        "financial_format": str(snapshot["format_version"]),
        "schema_versions": snapshot["schema_versions"],
        "schema_epoch": str(snapshot["workspace"]["schema_epoch"]),
        "workspace_mode": snapshot["workspace"]["mode"],
        "code_revision": source_manifest["code_revision"],
        "dirty": source_manifest["dirty"],
        "manifest_sha256": _sha(bundle.manifest_bytes),
        "data_sha256": _sha(bundle.data_bytes),
        "snapshot_witness": snapshot["snapshot_witness"],
        "projection_epoch": None
        if snapshot["format_version"] == 1
        else str(snapshot["workspace"]["projection_epoch"]),
        "snapshot_time_state": "UNKNOWN_NOT_RECORDED",
        "snapshot_at": None,
    }
    return _build_selected(
        snapshot, source, payout_ids=payout_ids, journal_ids=journal_ids, pinned_reads=pinned_reads
    )


def _build_selected(snapshot, source, *, payout_ids=(), journal_ids=(), pinned_reads=(), minor=0):
    selected = sorted({uuid(value) for value in payout_ids})
    requested_journals = sorted({uuid(value) for value in journal_ids})
    require(selected or requested_journals, "EMPTY_SELECTION")
    require(len(selected) <= 1000 and len(requested_journals) <= 1000, "SELECTION_LIMIT")
    records = {name: [] for name in TABLE_FIELDS}
    operations = {row["operation_id"]: row for row in snapshot["operations"]}
    payouts = {row["operation_id"]: row for row in snapshot.get("payouts", [])}
    require(set(selected) <= payouts.keys(), "SELECTED_PAYOUT_NOT_FOUND")
    for identifier in selected:
        original = payouts[identifier]
        record = _row(
            original,
            TABLE_FIELDS["payouts"],
            payout_id=identifier,
            intent_sha256=operations[identifier]["payload_sha256"],
            source_sequence=None
            if snapshot["format_version"] < 6
            else str(original["source_sequence"]),
            source_sequence_state="UNKNOWN_SOURCE_FORMAT_PRE_V6"
            if snapshot["format_version"] < 6
            else "RETAINED_SOURCE_SEQUENCE",
        )
        records["payouts"].append(record)
    headers = [
        row
        for row in snapshot["journals"]
        if row["journal_id"] in requested_journals or row["operation_id"] in selected
    ]
    require(
        set(requested_journals) <= {row["journal_id"] for row in headers},
        "SELECTED_JOURNAL_NOT_FOUND",
    )
    included_journals = {row["journal_id"] for row in headers}
    records["journals"] = [
        _row(
            row,
            TABLE_FIELDS["journals"],
            intent_sha256=operations[row["operation_id"]]["payload_sha256"],
        )
        for row in headers
    ]
    records["entries"] = [
        _row(row, TABLE_FIELDS["entries"])
        for row in snapshot["entries"]
        if row["journal_id"] in included_journals
    ]
    table_sources = {
        "holds": "reservations",
        "observations": "provider_observations",
        "resolutions": "payout_resolutions",
        "exceptions": "payout_incidents",
        "related_exceptions": "related_incidents",
        "dispositions": "payout_incident_resolutions",
    }
    for table, source_table in table_sources.items():
        for row in snapshot.get(source_table, []):
            if row["payout_id"] not in selected:
                continue
            changes = {}
            if table == "observations":
                changes = {"external_event_at": None, "external_event_time_state": EXTERNAL_TIME}
            elif table == "resolutions":
                changes = {"financial_commit_at": None, "financial_commit_time_state": COMMIT_TIME}
            records[table].append(_row(row, TABLE_FIELDS[table], **changes))
    account_ids = {row["account_id"] for row in records["entries"] + records["holds"]}
    account_ids.update(row["merchant_account_id"] for row in records["payouts"])
    for row in snapshot["accounts"]:
        if row["id"] in account_ids:
            records["accounts"].append(
                _row(
                    row, TABLE_FIELDS["accounts"], account_id=row["id"], account_class=row["class"]
                )
            )
    records["reconciliation_references"] = [_reconciliation(raw, source) for raw in pinned_reads]
    for table, rows in records.items():
        rows.sort(key=lambda row: tuple(row[name] for name in SORT_FIELDS[table]))
    selection = {
        "payout_ids": selected,
        "requested_journal_ids": requested_journals,
        "closure": "REFERENTIAL_CLOSURE_V1",
        "cutoff": "SOURCE_REPEATABLE_READ_SNAPSHOT_MEMBERSHIP",
    }
    records_raw = canonical(records)
    identity = _sha(
        _compact({"source": source, "selection": selection, "records_sha256": _sha(records_raw)})
    )
    manifest = {
        "format": "ledgerops.synthetic-evidence",
        "schema_major": 1,
        "schema_minor": minor,
        "semantics": "selected-financial-facts:v1",
        "dataset_id": str(uuid5(NAMESPACE_URL, "ledgerops.exchange:v1:" + identity)),
        "synthetic": True,
        "authority": "READ_ONLY_NO_CONTROL_CAPABILITY",
        "units": {"currency": "PLN", "money_unit": "PLN_minor", "currency_scale": 2},
        "selection": selection,
        "source": source,
        "records": {
            "file": "records.json",
            "bytes": str(len(records_raw)),
            "sha256": _sha(records_raw),
            "row_counts": {table: str(len(rows)) for table, rows in records.items()},
        },
    }
    validate(manifest, records)
    manifest_raw = canonical(manifest)
    require(len(records_raw) + len(manifest_raw) <= MAX_BYTES, "EXCHANGE_BYTE_LIMIT")
    return manifest_raw, records_raw


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_exchange(bundle, output_root, *, payout_ids=(), journal_ids=(), pinned_reads=()):
    "Publish a new immutable directory or recover the identical prior publication."
    manifest_raw, records_raw = build_exchange(
        bundle, payout_ids=payout_ids, journal_ids=journal_ids, pinned_reads=pinned_reads
    )
    return _publish_bytes(manifest_raw, records_raw, output_root)


def _publish_bytes(manifest_raw, records_raw, output_root):
    manifest = decode(manifest_raw)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    require(not output_root.is_symlink() and output_root.is_dir(), "INVALID_OUTPUT_ROOT")
    final = output_root / manifest["dataset_id"]
    if final.exists() or final.is_symlink():
        old_manifest, old_records, _ = load_exchange(final)
        require(
            canonical(old_manifest) == manifest_raw and canonical(old_records) == records_raw,
            "EXCHANGE_IDENTITY_CONFLICT",
        )
        _sync_directory(output_root)
        return final
    temporary = output_root / f".exchange-{uuid4()}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        for name, raw in (("manifest.json", manifest_raw), ("records.json", records_raw)):
            descriptor = os.open(
                temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
        _sync_directory(temporary)
        load_exchange(temporary)
        try:
            os.rename(temporary, final)
        except OSError:
            if not final.exists():
                raise
            old_manifest, old_records, _ = load_exchange(final)
            require(
                canonical(old_manifest) == manifest_raw and canonical(old_records) == records_raw,
                "EXCHANGE_IDENTITY_CONFLICT",
            )
        _sync_directory(output_root)
        return final
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_stream_exchange(
    path, *, payout_ids=(), journal_ids=(), pinned_reads=(), scratch_root=None
):
    """Explicit exchange minor1 provenance for complete financial stream10 sources."""
    from ledgerops.stream_verification import stage_stream, verify_store

    selected = sorted({uuid(value) for value in payout_ids})
    requested = sorted({uuid(value) for value in journal_ids})
    require(
        (selected or requested) and len(selected) <= 1000 and len(requested) <= 1000,
        "INVALID_SELECTION",
    )
    with stage_stream(path, scratch_root=scratch_root) as store:
        require(
            store.source.get("capture_context") is None,
            "EXCHANGE_REQUIRES_REPEATABLE_READ_SOURCE",
        )
        report = verify_store(store)
        snapshot = {"format_version": 9, **store.source}
        for table in (
            "accounts",
            "operations",
            "journals",
            "entries",
            "payouts",
            "reservations",
            "provider_observations",
            "payout_resolutions",
            "payout_incidents",
            "related_incidents",
            "payout_incident_resolutions",
        ):
            snapshot[table] = []
        byte_count = 0

        def append(table, row, names):
            nonlocal byte_count
            projected = {name: row[name] for name in names if name in row}
            byte_count += len(canonical(projected))
            require(byte_count <= MAX_BYTES, "SELECTED_RECORD_BYTE_LIMIT")
            snapshot[table].append(projected)

        for identifier in selected:
            row = store.one("payouts", "operation_id", identifier)
            require(row is not None, "SELECTED_PAYOUT_NOT_FOUND")
            append("payouts", row, set(TABLE_FIELDS["payouts"].split()) | {"operation_id"})
        headers = {}
        for identifier in requested:
            row = store.one("journals", "journal_id", identifier)
            require(row is not None, "SELECTED_JOURNAL_NOT_FOUND")
            headers[row["journal_id"]] = row
        for identifier in selected:
            for row in store.rows("journals", "operation_id=?", (identifier,)):
                headers[row["journal_id"]] = row
        op_ids = set(selected)
        for row in headers.values():
            append("journals", row, TABLE_FIELDS["journals"].split())
            op_ids.add(row["operation_id"])
            for entry in store.rows("entries", "journal_id=?", (row["journal_id"],)):
                append("entries", entry, TABLE_FIELDS["entries"].split())
        for identifier in op_ids:
            row = store.one("operations", "operation_id", identifier)
            require(row is not None, "SELECTED_OPERATION_NOT_FOUND")
            append("operations", row, ("operation_id", "payload_sha256"))
        sources = {
            "reservations": "holds",
            "provider_observations": "observations",
            "payout_resolutions": "resolutions",
            "payout_incidents": "exceptions",
            "related_incidents": "related_exceptions",
            "payout_incident_resolutions": "dispositions",
        }
        for table, output in sources.items():
            for identifier in selected:
                for row in store.rows(table, "payout_id=?", (identifier,)):
                    append(table, row, TABLE_FIELDS[output].split())
        account_ids = {row["account_id"] for row in snapshot["entries"] + snapshot["reservations"]}
        account_ids.update(row["merchant_account_id"] for row in snapshot["payouts"])
        for identifier in account_ids:
            row = store.one("accounts", "id", identifier)
            require(row is not None, "SELECTED_ACCOUNT_NOT_FOUND")
            append("accounts", row, ("id", "owner_id", "class", "normal_side", "currency"))
        full = {
            name: report[name]
            for name in (
                "structure_ok",
                "projection_ok",
                "request_intent_ok",
                "reservation_supported",
                "verification_complete",
                "healthy",
            )
        }
        full.update(
            report_version=4,
            report_sha256=_sha(canonical(report)),
            external_consistency_ok=report["external_evidence"]["external_consistency_ok"],
        )
        source = {
            "workspace_id": store.workspace,
            "export_id": store.manifest["export_id"],
            "financial_format": "10",
            "schema_versions": store.source["schema_versions"],
            "schema_epoch": str(store.source["workspace"]["schema_epoch"]),
            "workspace_mode": store.source["workspace"]["mode"],
            "code_revision": store.manifest["code_revision"],
            "dirty": store.manifest["dirty"],
            "manifest_sha256": _sha(store.manifest_raw),
            "data_sha256": store.content_sha256,
            "snapshot_witness": store.source["snapshot_witness"],
            "projection_epoch": str(store.source["workspace"]["projection_epoch"]),
            "snapshot_time_state": "UNKNOWN_NOT_RECORDED",
            "snapshot_at": None,
            "source_kind": "FINANCIAL_STREAM10",
            "row_format": "9",
            "chunk_count": str(len(store.manifest["chunks"])),
            "content_hash_semantics": "MANIFEST_ORDER_CONCATENATED_JSONL_BYTES",
            "full_verification": full,
        }
        return _build_selected(
            snapshot,
            source,
            payout_ids=selected,
            journal_ids=requested,
            pinned_reads=pinned_reads,
            minor=1,
        )


def publish_stream_exchange(
    path, output_root, *, payout_ids=(), journal_ids=(), pinned_reads=(), scratch_root=None
):
    raw_manifest, raw_records = build_stream_exchange(
        path,
        payout_ids=payout_ids,
        journal_ids=journal_ids,
        pinned_reads=pinned_reads,
        scratch_root=scratch_root,
    )
    return _publish_bytes(raw_manifest, raw_records, output_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--financial-export", type=Path)
    source.add_argument("--financial-stream", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--payout-id", action="append", default=[])
    parser.add_argument("--journal-id", action="append", default=[])
    parser.add_argument("--pinned-read", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    try:
        pinned = []
        for path in args.pinned_read:
            require(
                not path.is_symlink() and path.is_file() and path.stat().st_size <= _MAX_PIN_BYTES,
                "INVALID_PINNED_READ_FILE",
            )
            pinned.append(path.read_bytes())
        if args.financial_stream:
            result = publish_stream_exchange(
                args.financial_stream,
                args.output_root,
                payout_ids=args.payout_id,
                journal_ids=args.journal_id,
                scratch_root=args.scratch_root,
                pinned_reads=pinned,
            )
            print(json.dumps({"exchange_path": str(result)}, sort_keys=True))
            return 0
        result = publish_exchange(
            load_export_bundle(args.financial_export),
            args.output_root,
            payout_ids=args.payout_id,
            journal_ids=args.journal_id,
            pinned_reads=pinned,
        )
        print(json.dumps({"exchange_path": str(result)}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print(json.dumps({"error": "INVALID_EXCHANGE_INPUT_OR_CONFIGURATION"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
