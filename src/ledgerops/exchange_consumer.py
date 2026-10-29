"""Standalone stdlib reader for immutable selected synthetic evidence v1.

Copy this file and an exchange directory to a machine without LedgerOps. It has no
network, database, credentials or control-plane entry point. Validation establishes
schema/hash/reference consistency, not historical authorization or provider truth.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10_000
TABLE_FIELDS = {
    "accounts": "account_id owner_id account_class normal_side currency",
    "payouts": (
        "payout_id merchant_account_id beneficiary_id provider_namespace "
        "provider_key principal_minor fee_minor gross_minor currency "
        "contract_version dispatch_deadline knowledge version "
        "source_sequence source_sequence_state intent_sha256"
    ),
    "holds": "payout_id account_id gross_minor state version",
    "journals": ("journal_id operation_id kind line_count total_minor recorded_at intent_sha256"),
    "entries": "journal_id line_no account_id side amount_minor",
    "observations": (
        "payout_id observation_id provider_namespace event_id kind state "
        "source_sequence envelope_sha256 received_at external_event_at "
        "external_event_time_state"
    ),
    "resolutions": (
        "payout_id observation_id state journal_id resolved_at "
        "financial_commit_at financial_commit_time_state"
    ),
    "exceptions": (
        "incident_id payout_id category code observation_id "
        "related_observation_id related_payload_sha256 created_at"
    ),
    "related_exceptions": (
        "incident_id payout_id observation_id code related_payload_sha256 created_at"
    ),
    "dispositions": "incident_id payout_id action_id observation_id resolved_at",
    "reconciliation_references": (
        "run_id provider_namespace business_date timezone start_inclusive "
        "end_exclusive effect_watermark assignment_watermark "
        "statement_version result_sha256 semantic_sha256 "
        "input_manifest_sha256 partition_plan_sha256 ledger_data_sha256 "
        "statement_sha256 evidence_epoch_at_pin published_epoch "
        "is_current_at_pin reader_pin_id pinned_read_sha256 reference_status"
    ),
}
SORT_FIELDS = {
    "accounts": ("account_id",),
    "payouts": ("payout_id",),
    "holds": ("payout_id",),
    "journals": ("journal_id",),
    "entries": ("journal_id", "line_no"),
    "observations": ("observation_id",),
    "resolutions": ("payout_id",),
    "exceptions": ("incident_id",),
    "related_exceptions": ("incident_id", "observation_id"),
    "dispositions": ("incident_id",),
    "reconciliation_references": ("run_id",),
}
EXTERNAL_TIME = "UNKNOWN_PROVIDER_CONTRACT_NO_TIMESTAMP"
COMMIT_TIME = "UNKNOWN_NOT_RECORDED"


class ExchangeError(ValueError):
    """Finite input error; raw source records are never included in exceptions."""


def require(condition, code):
    if not condition:
        raise ExchangeError(code)


def canonical(value):
    try:
        return (
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )
            + "\n"
        ).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ExchangeError("INVALID_JSON_VALUE") from exc


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "DUPLICATE_JSON_FIELD")
            value[key] = item
        return value

    def bad_constant(_):
        raise ExchangeError("NONFINITE_JSON")

    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=bad_constant)
        require(canonical(value) == raw, "NONCANONICAL_JSON")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ExchangeError):
            raise
        raise ExchangeError("INVALID_JSON") from exc


def fields(value, names):
    require(type(value) is dict and value.keys() == set(names.split()), "UNKNOWN_OR_MISSING_FIELDS")


def uuid(value):
    try:
        require(type(value) is str and str(UUID(value)) == value, "INVALID_UUID")
    except (ValueError, AttributeError) as exc:
        raise ExchangeError("INVALID_UUID") from exc
    return value


def digest(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "INVALID_SHA256")
    return value


def number(value, minimum=0, maximum=2**63 - 1):
    require(
        type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,37}", value),
        "LOSSLESS_INTEGER_STRING_REQUIRED",
    )
    parsed = int(value)
    require(minimum <= parsed <= maximum, "INTEGER_OUT_OF_BOUNDS")
    return parsed


def instant(value):
    try:
        require(type(value) is str and len(value) <= 40, "INVALID_UTC_INSTANT")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
        require(offset is not None and offset.total_seconds() == 0, "INVALID_UTC_INSTANT")
    except ValueError as exc:
        raise ExchangeError("INVALID_UTC_INSTANT") from exc


def text(value):
    require(
        type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value), "INVALID_IDENTIFIER"
    )


def read_file(directory, name, maximum):
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum, "INVALID_MEMBER_SIZE")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum + 1)
        require(len(raw) == info.st_size, "CHANGING_MEMBER")
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _manifest(manifest):
    fields(
        manifest,
        (
            "format schema_major schema_minor semantics dataset_id synthetic "
            "authority units selection source records"
        ),
    )
    require(
        manifest["format"] == "ledgerops.synthetic-evidence"
        and type(manifest["schema_major"]) is int
        and manifest["schema_major"] == 1
        and type(manifest["schema_minor"]) is int
        and manifest["schema_minor"] in (0, 1),
        "UNSUPPORTED_SCHEMA_VERSION",
    )
    require(
        manifest["semantics"] == "selected-financial-facts:v1"
        and manifest["synthetic"] is True
        and manifest["authority"] == "READ_ONLY_NO_CONTROL_CAPABILITY",
        "UNSUPPORTED_SEMANTICS",
    )
    uuid(manifest["dataset_id"])
    require(
        manifest["units"] == {"currency": "PLN", "money_unit": "PLN_minor", "currency_scale": 2}
        and type(manifest["units"]["currency_scale"]) is int,
        "UNSUPPORTED_UNITS",
    )
    selection, source, records = manifest["selection"], manifest["source"], manifest["records"]
    fields(selection, "payout_ids requested_journal_ids closure cutoff")
    require(
        selection["closure"] == "REFERENTIAL_CLOSURE_V1"
        and selection["cutoff"] == "SOURCE_REPEATABLE_READ_SNAPSHOT_MEMBERSHIP",
        "UNSUPPORTED_SELECTION",
    )
    for name in ("payout_ids", "requested_journal_ids"):
        values = selection[name]
        require(
            type(values) is list
            and len(values) <= 1000
            and [uuid(value) for value in values] == sorted(set(values)),
            "INVALID_SELECTION",
        )
    require(selection["payout_ids"] or selection["requested_journal_ids"], "EMPTY_SELECTION")
    source_fields = (
        "workspace_id export_id financial_format schema_versions "
        "schema_epoch workspace_mode code_revision dirty manifest_sha256 "
        "data_sha256 snapshot_witness projection_epoch snapshot_time_state snapshot_at"
    )
    if manifest["schema_minor"] == 1:
        source_fields += (
            " source_kind row_format chunk_count content_hash_semantics full_verification"
        )
    fields(source, source_fields)
    uuid(source["workspace_id"])
    uuid(source["export_id"])
    version = number(source["financial_format"], 1, 10 if manifest["schema_minor"] == 1 else 9)
    if manifest["schema_minor"] == 1:
        require(
            version == 10
            and source["source_kind"] == "FINANCIAL_STREAM10"
            and source["row_format"] == "9"
            and source["content_hash_semantics"] == "MANIFEST_ORDER_CONCATENATED_JSONL_BYTES",
            "UNSUPPORTED_STREAM_PROVENANCE",
        )
        number(source["chunk_count"], 1, 1000000)
        full = source["full_verification"]
        fields(
            full,
            "report_version report_sha256 structure_ok projection_ok request_intent_ok "
            "reservation_supported verification_complete healthy external_consistency_ok",
        )
        require(
            type(full["report_version"]) is int and full["report_version"] == 4,
            "UNSUPPORTED_SOURCE_REPORT",
        )
        digest(full["report_sha256"])
        flags = (
            "structure_ok",
            "projection_ok",
            "request_intent_ok",
            "reservation_supported",
            "verification_complete",
            "healthy",
            "external_consistency_ok",
        )
        require(all(type(full[name]) is bool for name in flags), "INVALID_SOURCE_REPORT_FLAGS")
        require(
            full["healthy"]
            == all(
                full[name]
                for name in (
                    "structure_ok",
                    "projection_ok",
                    "request_intent_ok",
                    "verification_complete",
                    "external_consistency_ok",
                )
            ),
            "INCONSISTENT_SOURCE_REPORT",
        )
    number(source["schema_epoch"], 1)
    require(source["workspace_mode"] in {"NORMAL", "FROZEN", "RESTORE"}, "UNSUPPORTED_SOURCE_MODE")
    schemas = [
        "001_foundation.sql",
        "002_export_reader.sql",
        "003_projection_maintenance.sql",
        "004_resume_review.sql",
        "005_full_reversal.sql",
        "006_payout_admission.sql",
        "007_payout_resolution.sql",
        "008_reconciliation_publication.sql",
        "009_authentication.sql",
        "010_operator_actions.sql",
    ]
    require(source["schema_versions"] == schemas[: version + 1], "UNSUPPORTED_SOURCE_SCHEMA")
    require(
        type(source["code_revision"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", source["code_revision"])
        and type(source["dirty"]) is bool,
        "INVALID_SOURCE_REVISION",
    )
    digest(source["manifest_sha256"])
    digest(source["data_sha256"])
    require(
        type(source["snapshot_witness"]) is str
        and len(source["snapshot_witness"]) <= 65536
        and re.fullmatch(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", source["snapshot_witness"]),
        "INVALID_SNAPSHOT_WITNESS",
    )
    if version >= 2:
        number(source["projection_epoch"], 1)
    else:
        require(source["projection_epoch"] is None, "UNSUPPORTED_SOURCE_CAPABILITY")
    require(
        source["snapshot_time_state"] == "UNKNOWN_NOT_RECORDED" and source["snapshot_at"] is None,
        "UNSUPPORTED_TEMPORAL_SEMANTICS",
    )
    fields(records, "file bytes sha256 row_counts")
    require(records["file"] == "records.json", "UNKNOWN_MEMBER")
    number(records["bytes"], 1, MAX_BYTES)
    digest(records["sha256"])
    fields(records["row_counts"], " ".join(TABLE_FIELDS))
    for value in records["row_counts"].values():
        number(value, 0, MAX_ROWS)


def validate(manifest, records):
    """Validate raw selected facts; no complete-ledger or external-truth claim."""
    _manifest(manifest)
    fields(records, " ".join(TABLE_FIELDS))
    for table, names in TABLE_FIELDS.items():
        rows = records[table]
        require(type(rows) is list and len(rows) <= MAX_ROWS, "TABLE_LIMIT")
        require(str(len(rows)) == manifest["records"]["row_counts"][table], "ROW_COUNT_MISMATCH")
        identities = []
        for row in rows:
            row_names = names
            if table == "reconciliation_references" and manifest["schema_minor"] == 1:
                row_names += " matcher_data_sha256 full_stream_manifest_sha256"
            fields(row, row_names)
            nullable = {
                "payouts": {"source_sequence"},
                "resolutions": {"journal_id", "financial_commit_at"},
                "observations": {"external_event_at"},
                "exceptions": {
                    "observation_id",
                    "related_observation_id",
                    "related_payload_sha256",
                },
                "reconciliation_references": {"published_epoch"},
            }.get(table, set())
            for name, value in row.items():
                require(value is not None or name in nullable, "UNSUPPORTED_NULL_SEMANTICS")
                if value is None:
                    continue
                if name.endswith("_id") or name == "provider_namespace":
                    if value is not None:
                        uuid(value)
                elif name.endswith("_sha256"):
                    if value is not None:
                        digest(value)
                elif name.endswith("_minor"):
                    number(value, 0 if name == "fee_minor" else 1, 10**38 - 1)
                elif name in {
                    "version",
                    "line_count",
                    "line_no",
                    "contract_version",
                    "statement_version",
                }:
                    number(value, 1)
                elif name in {
                    "source_sequence",
                    "effect_watermark",
                    "assignment_watermark",
                    "evidence_epoch_at_pin",
                }:
                    number(value)
                elif name == "published_epoch" and value is not None:
                    number(value, 1)
                elif (
                    name.endswith("_at")
                    and value is not None
                    or name in {"dispatch_deadline", "start_inclusive", "end_exclusive"}
                ):
                    instant(value)
            identity = tuple(row[name] for name in SORT_FIELDS[table])
            identities.append(identity)
        require(identities == sorted(set(identities)), "UNSORTED_OR_DUPLICATE_IDENTITY")
    accounts = {row["account_id"]: row for row in records["accounts"]}
    payouts = {row["payout_id"]: row for row in records["payouts"]}
    journals = {row["journal_id"]: row for row in records["journals"]}
    observations = {row["observation_id"]: row for row in records["observations"]}
    incidents = {row["incident_id"]: row for row in records["exceptions"]}
    require(sorted(payouts) == manifest["selection"]["payout_ids"], "PAYOUT_SELECTION_MISMATCH")
    require(
        set(manifest["selection"]["requested_journal_ids"]) <= journals.keys(),
        "JOURNAL_SELECTION_MISSING",
    )
    used_accounts = set()
    for row in accounts.values():
        require(
            row["currency"] == "PLN"
            and row["account_class"]
            in {"SYNTHETIC_CLEARING", "CUSTOMER_LIABILITY", "MERCHANT_LIABILITY"}
            and row["normal_side"]
            == ("D" if row["account_class"] == "SYNTHETIC_CLEARING" else "C"),
            "INVALID_ACCOUNT",
        )
    for row in payouts.values():
        require(
            row["merchant_account_id"] in accounts
            and accounts[row["merchant_account_id"]]["account_class"] == "MERCHANT_LIABILITY",
            "PAYOUT_OWNER_REFERENCE_MISSING",
        )
        used_accounts.add(row["merchant_account_id"])
        require(
            row["currency"] == "PLN"
            and row["contract_version"] == "1"
            and int(row["principal_minor"]) + int(row["fee_minor"]) == int(row["gross_minor"])
            and row["knowledge"]
            in {"NOT_SENT", "UNKNOWN", "ACCEPTED", "SUCCEEDED", "FAILED", "CLOSED_ABSENT"},
            "UNSUPPORTED_PAYOUT_SEMANTICS",
        )
        if int(manifest["source"]["financial_format"]) >= 6:
            require(
                row["source_sequence_state"] == "RETAINED_SOURCE_SEQUENCE"
                and row["source_sequence"] is not None,
                "UNSUPPORTED_SEQUENCE_SEMANTICS",
            )
        else:
            require(
                row["source_sequence_state"] == "UNKNOWN_SOURCE_FORMAT_PRE_V6"
                and row["source_sequence"] is None,
                "UNSUPPORTED_SEQUENCE_SEMANTICS",
            )
        expected_key = f"v1.{row['provider_namespace']}.{manifest['source']['workspace_id']}"
        require(
            row["provider_key"] == expected_key + "." + row["payout_id"],
            "PROVIDER_KEY_IDENTITY_MISMATCH",
        )
    for table in (
        "holds",
        "observations",
        "resolutions",
        "exceptions",
        "related_exceptions",
        "dispositions",
    ):
        for row in records[table]:
            require(row["payout_id"] in payouts, "UNSELECTED_PAYOUT_REFERENCE")
    for row in records["holds"]:
        payout = payouts[row["payout_id"]]
        require(
            row["account_id"] == payout["merchant_account_id"]
            and row["gross_minor"] == payout["gross_minor"]
            and row["state"] in {"HELD", "CONSUMED", "RELEASED"},
            "INVALID_HOLD",
        )
        used_accounts.add(row["account_id"])
    entries_by_journal = {}
    for entry in records["entries"]:
        entries_by_journal.setdefault(entry["journal_id"], []).append(entry)
    for row in journals.values():
        require(
            row["kind"] in {"OPENING", "PURCHASE", "REVERSAL", "PAYOUT"}, "UNSUPPORTED_JOURNAL_KIND"
        )
        require(
            row["operation_id"] in payouts
            or row["journal_id"] in manifest["selection"]["requested_journal_ids"],
            "UNSELECTED_JOURNAL",
        )
        entries = entries_by_journal.get(row["journal_id"], [])
        require(
            len(entries) == int(row["line_count"]) and len(entries) >= 2,
            "JOURNAL_LINE_COUNT_MISMATCH",
        )
        debit = sum(int(entry["amount_minor"]) for entry in entries if entry["side"] == "D")
        credit = sum(int(entry["amount_minor"]) for entry in entries if entry["side"] == "C")
        require(debit == credit == int(row["total_minor"]), "UNBALANCED_JOURNAL")
    for row in records["entries"]:
        require(
            row["journal_id"] in journals
            and row["account_id"] in accounts
            and row["side"] in {"D", "C"},
            "INVALID_ENTRY_REFERENCE",
        )
        used_accounts.add(row["account_id"])
    require(used_accounts == accounts.keys(), "UNSELECTED_ACCOUNT")
    for row in observations.values():
        require(
            row["provider_namespace"] == payouts[row["payout_id"]]["provider_namespace"]
            and row["kind"] in {"lookup", "execution"}
            and row["state"] in {"NOT_FOUND", "ACCEPTED", "SUCCEEDED", "FAILED", "CLOSED_ABSENT"},
            "UNSUPPORTED_OBSERVATION_SEMANTICS",
        )
        require(
            row["external_event_at"] is None and row["external_event_time_state"] == EXTERNAL_TIME,
            "UNSUPPORTED_TEMPORAL_SEMANTICS",
        )
    for row in records["resolutions"]:
        observation = observations.get(row["observation_id"])
        require(
            observation is not None
            and observation["payout_id"] == row["payout_id"]
            and row["state"] == observation["state"]
            and row["state"] in {"SUCCEEDED", "FAILED", "CLOSED_ABSENT"},
            "INVALID_RESOLUTION_REFERENCE",
        )
        require(
            (row["state"] == "SUCCEEDED" and row["journal_id"] in journals)
            or (row["state"] != "SUCCEEDED" and row["journal_id"] is None),
            "INVALID_RESOLUTION_JOURNAL",
        )
        require(
            row["financial_commit_at"] is None
            and row["financial_commit_time_state"] == COMMIT_TIME,
            "UNSUPPORTED_TEMPORAL_SEMANTICS",
        )
    for row in incidents.values():
        require(
            row["category"] in {"LOCAL_BLOCKER", "PROVIDER_CONTRADICTION", "EVIDENCE_LIMIT"},
            "UNSUPPORTED_EXCEPTION_CATEGORY",
        )
        text(row["code"])
        if row["observation_id"] is not None:
            require(
                row["observation_id"] in observations
                and observations[row["observation_id"]]["payout_id"] == row["payout_id"],
                "INVALID_EXCEPTION_OBSERVATION",
            )
    for row in records["related_exceptions"]:
        text(row["code"])
        require(
            row["observation_id"] in observations
            and observations[row["observation_id"]]["payout_id"] == row["payout_id"],
            "INVALID_RELATED_EXCEPTION",
        )
    for row in records["dispositions"]:
        original = incidents.get(row["incident_id"])
        require(
            original is not None
            and original["category"] == "LOCAL_BLOCKER"
            and original["payout_id"] == row["payout_id"]
            and original["observation_id"] == row["observation_id"],
            "INVALID_DISPOSITION_REFERENCE",
        )
    for row in records["reconciliation_references"]:
        require(
            row["timezone"] == "Europe/Warsaw"
            and type(row["business_date"]) is str
            and date.fromisoformat(row["business_date"]).isoformat() == row["business_date"],
            "UNSUPPORTED_BUSINESS_DATE",
        )
        expected_status = "HISTORICAL_PIN_REFERENCE_NOT_LIVE_AUTHORITY"
        if manifest["schema_minor"] == 1:
            expected_status = "HISTORICAL_STREAM_PIN_REFERENCE_NOT_LIVE_AUTHORITY"
            require(
                row["full_stream_manifest_sha256"] == manifest["source"]["manifest_sha256"],
                "INVALID_STREAM_MANIFEST_REFERENCE",
            )
        require(
            row["ledger_data_sha256"] == manifest["source"]["data_sha256"]
            and type(row["is_current_at_pin"]) is bool
            and row["reference_status"] == expected_status,
            "INVALID_RECONCILIATION_REFERENCE",
        )
    identity = hashlib.sha256(
        canonical(
            {
                "source": manifest["source"],
                "selection": manifest["selection"],
                "records_sha256": manifest["records"]["sha256"],
            }
        )[:-1]
    ).hexdigest()
    require(
        manifest["dataset_id"] == str(uuid5(NAMESPACE_URL, "ledgerops.exchange:v1:" + identity)),
        "DATASET_IDENTITY_MISMATCH",
    )
    result = {
        "dataset_id": manifest["dataset_id"],
        "workspace_id": manifest["source"]["workspace_id"],
        "validation_scope": "SCHEMA_HASH_REFERENTIAL_SELECTION_ONLY",
        "source_data_sha256": manifest["source"]["data_sha256"],
        "source_code_revision": manifest["source"]["code_revision"],
        "payout_count": str(len(payouts)),
        "journal_count": str(len(journals)),
        "external_event_time": EXTERNAL_TIME,
        "financial_commit_time": COMMIT_TIME,
        "provider_authenticity": "NOT_VERIFIED_NO_TRUST_KEYS",
        "historical_authorization": "NOT_EXPORTED_NOT_VERIFIED",
        "journal_totals": [
            {
                "journal_id": row["journal_id"],
                "debit_minor": row["total_minor"],
                "credit_minor": row["total_minor"],
            }
            for row in records["journals"]
        ],
    }
    if manifest["schema_minor"] == 1:
        full = manifest["source"]["full_verification"]
        result.update(
            source_kind="FINANCIAL_STREAM10",
            source_healthy=full["healthy"],
            source_verification_complete=full["verification_complete"],
        )
    return result


def load_exchange(path):
    descriptor = None
    try:
        path = Path(path)
        require(not path.is_symlink(), "SYMLINK_DIRECTORY")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        require(
            set(os.listdir(descriptor)) == {"manifest.json", "records.json"},
            "UNKNOWN_OR_MISSING_MEMBERS",
        )
        manifest_raw = read_file(descriptor, "manifest.json", 1024 * 1024)
        manifest = decode(manifest_raw)
        _manifest(manifest)
        records_raw = read_file(descriptor, "records.json", MAX_BYTES)
        require(len(manifest_raw) + len(records_raw) <= MAX_BYTES, "EXCHANGE_BYTE_LIMIT")
        require(
            str(len(records_raw)) == manifest["records"]["bytes"]
            and hashlib.sha256(records_raw).hexdigest() == manifest["records"]["sha256"],
            "RECORDS_HASH_OR_SIZE_MISMATCH",
        )
        records = decode(records_raw)
        return manifest, records, validate(manifest, records)
    except (OSError, KeyError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ExchangeError):
            raise
        raise ExchangeError("INVALID_EXCHANGE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(load_exchange(args.path)[2], sort_keys=True, separators=(",", ":")))
        return 0
    except ExchangeError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
