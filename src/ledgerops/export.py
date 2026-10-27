"""Bounded, single-snapshot PostgreSQL exports and immutable local JSON bundles.

The files preserve database observations; they do not prove business correctness.
Read with a dedicated ledger_reader connection. Only completed UUID directories
are accepted, and exported payload bytes remain lossless lowercase hexadecimal.
"""

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

MAX_ROWS_PER_TABLE = 10_000
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
SCHEMA_VERSIONS_BY_FORMAT = {
    1: ("001_foundation.sql", "002_export_reader.sql"),
    2: ("001_foundation.sql", "002_export_reader.sql", "003_projection_maintenance.sql"),
}
SCHEMA_VERSIONS = SCHEMA_VERSIONS_BY_FORMAT[2]
_BASE_TABLES = {
    "accounts": ("account", "workspace_id,id,owner_id,class,normal_side,currency,enabled", "id"),
    "balances": (
        "account_balance",
        "workspace_id,account_id,normal_side,debits,credits,reserved,booked,version",
        "account_id",
    ),
    "operations": (
        "operation",
        "workspace_id,operation_id,kind,payload,payload_sha256,admission,http_status,"
        "response_body,created_at",
        "operation_id",
    ),
    "requests": (
        "request_result",
        "workspace_id,client_id,route_family,key,operation_id,payload,payload_sha256,"
        "http_status,response_body",
        'client_id,route_family COLLATE "C",key COLLATE "C"',
    ),
    "journals": (
        "journal",
        "workspace_id,journal_id,operation_id,kind,line_count,total_minor,recorded_at",
        "journal_id",
    ),
    "entries": (
        "journal_entry",
        "workspace_id,journal_id,line_no,account_id,side,amount_minor",
        "journal_id,line_no",
    ),
}
_MAINTENANCE_TABLES = {
    "maintenance_freezes": (
        "maintenance_freeze",
        "workspace_id,freeze_id,actor_id,reason,frozen_at,resumed_at,resumed_by,resume_reason",
        "freeze_id",
    ),
    "repairs": (
        "projection_repair",
        "workspace_id,repair_id,freeze_id,actor_id,reason,preview_export_id,source_witness,"
        "old_epoch,new_epoch,account_count,applied_at",
        "repair_id",
    ),
    "repair_rows": (
        "projection_repair_row",
        "workspace_id,repair_id,account_id,normal_side,before_debits,before_credits,"
        "before_reserved,before_booked,before_version,after_debits,after_credits,"
        "after_reserved,after_booked,after_version",
        "repair_id,account_id",
    ),
}
_TABLES_BY_FORMAT = {1: _BASE_TABLES, 2: {**_BASE_TABLES, **_MAINTENANCE_TABLES}}
_TABLES = _TABLES_BY_FORMAT[2]
_NULLABLE_FIELDS = frozenset(
    {
        "current_freeze_id",
        "resumed_at",
        "resumed_by",
        "resume_reason",
        "before_debits",
        "before_credits",
        "before_reserved",
        "before_booked",
        "before_version",
    }
)
_MONEY_FIELDS = frozenset(
    {
        "debits",
        "credits",
        "reserved",
        "booked",
        "total_minor",
        "amount_minor",
        "before_debits",
        "before_credits",
        "before_reserved",
        "before_booked",
        "after_debits",
        "after_credits",
        "after_reserved",
        "after_booked",
    }
)
_UUID_FIELDS = frozenset(
    {
        "id",
        "workspace_id",
        "owner_id",
        "account_id",
        "operation_id",
        "client_id",
        "journal_id",
        "current_freeze_id",
        "freeze_id",
        "actor_id",
        "resumed_by",
        "repair_id",
        "preview_export_id",
    }
)
_INT_FIELDS = {
    "schema_epoch": (1, 2**63 - 1),
    "projection_epoch": (1, 2**63 - 1),
    "old_epoch": (1, 2**63 - 2),
    "new_epoch": (2, 2**63 - 1),
    "before_version": (0, 2**63 - 2),
    "after_version": (1, 2**63 - 1),
    "account_count": (0, 10_000),
    "version": (0, 2**63 - 1),
    "line_count": (2, 16),
    "line_no": (1, 16),
}
_ENUM_FIELDS = {
    "currency": {"PLN"},
    "mode": {"NORMAL", "FROZEN", "RESTORE"},
    "class": {"SYNTHETIC_CLEARING", "CUSTOMER_LIABILITY", "MERCHANT_LIABILITY"},
    "normal_side": {"D", "C"},
    "side": {"D", "C"},
    "kind": {"OPENING", "PURCHASE"},
    "admission": {"ACCEPTED", "REJECTED"},
    "route_family": {"purchase:v1"},
}
_ROW_KEYS = {
    name: frozenset(fields.split(",")) - {"payload"}
    | ({"payload_hex"} if "payload" in fields.split(",") else set())
    for name, (_, fields, _) in _TABLES.items()
}
_SORT_KEYS = {
    "accounts": ("id",),
    "balances": ("account_id",),
    "operations": ("operation_id",),
    "requests": ("client_id", "route_family", "key"),
    "journals": ("journal_id",),
    "entries": ("journal_id", "line_no"),
    "maintenance_freezes": ("freeze_id",),
    "repairs": ("repair_id",),
    "repair_rows": ("repair_id", "account_id"),
}
_DATA_KEYS_BY_FORMAT = {
    version: frozenset(
        {
            "format_version",
            "workspace",
            "snapshot_witness",
            "schema_versions",
            "server_version",
            *tables,
        }
    )
    for version, tables in _TABLES_BY_FORMAT.items()
}
_MANIFEST_KEYS = frozenset(
    {
        "format_version",
        "export_id",
        "workspace_id",
        "code_revision",
        "dirty",
        "data_bytes",
        "data_sha256",
        "row_counts",
    }
)
_INTEGER_TEXT = re.compile(r"0|[1-9][0-9]*", re.ASCII)


class ExportError(ValueError):
    """The snapshot or bundle does not meet the declared export contract."""


class ExportLimitExceeded(ExportError):
    """The complete snapshot exceeds a declared bound; no truncated result is valid."""


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ExportError("Expected canonical UUID text") from exc
    return value


def _json_bytes(value):
    try:
        return (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ExportError("Export contains a non-JSON value") from exc


def _normal_row(row):
    result = {}
    for key, value in row.items():
        if key == "payload":
            result["payload_hex"] = bytes(value).hex()
        elif key == "payload_sha256":
            result[key] = bytes(value).hex()
        elif value is None and key in _NULLABLE_FIELDS:
            result[key] = None
        elif key in _MONEY_FIELDS:
            if type(value) is int:
                result[key] = str(value)
            elif (
                isinstance(value, Decimal)
                and value.is_finite()
                and value == value.to_integral_value()
            ):
                result[key] = str(int(value))
            else:
                raise ExportError("Money must be an exact finite integer")
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            if value.tzinfo is None:
                raise ExportError("Timestamp must include a timezone")
            result[key] = (
                value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
        else:
            result[key] = value
    return result


def _validate_field(key, value):
    if value is None and key in _NULLABLE_FIELDS:
        return
    if key in _UUID_FIELDS:
        _uuid(value)
    elif key in _MONEY_FIELDS:
        maximum = 9_000_000_000_000_000 if key == "amount_minor" else 10**38 - 1
        minimum = 1 if key in {"amount_minor", "total_minor"} else 0
        if (
            type(value) is not str
            or len(value) > 38
            or not _INTEGER_TEXT.fullmatch(value)
            or not minimum <= int(value) <= maximum
        ):
            raise ExportError("Invalid canonical integer money field")
    elif key in _INT_FIELDS:
        low, high = _INT_FIELDS[key]
        if type(value) is not int or not low <= value <= high:
            raise ExportError("Invalid integer field")
    elif key == "http_status":
        if type(value) is not int or value not in {201, 422}:
            raise ExportError("Invalid terminal result status")
    elif key in _ENUM_FIELDS:
        if type(value) is not str or value not in _ENUM_FIELDS[key]:
            raise ExportError("Unsupported export enum")
    elif key == "enabled":
        if type(value) is not bool:
            raise ExportError("Account enabled must be boolean")
    elif key == "response_body":
        if type(value) is not dict:
            raise ExportError("Terminal response must be an object")
    elif key in {"payload_hex", "payload_sha256"}:
        pattern = r"[0-9a-f]{64}" if key == "payload_sha256" else r"(?:[0-9a-f]{2})+"
        if type(value) is not str or not re.fullmatch(pattern, value):
            raise ExportError("Invalid hexadecimal payload field")
    elif key == "key":
        try:
            valid = type(value) is str and "\x00" not in value and 1 <= len(value.encode()) <= 128
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ExportError("Invalid request key")
    elif key in {"created_at", "recorded_at", "frozen_at", "resumed_at", "applied_at"}:
        try:
            if type(value) is not str or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
            ):
                raise ValueError
            datetime.fromisoformat(value)
        except (ValueError, TypeError) as exc:
            raise ExportError("Invalid UTC timestamp") from exc
    elif key in {"reason", "resume_reason"}:
        try:
            valid = type(value) is str and "\x00" not in value and 1 <= len(value.encode()) <= 256
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise ExportError("Invalid maintenance reason")
    elif key == "source_witness":
        if type(value) is not str or not re.fullmatch(
            r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", value
        ):
            raise ExportError("Invalid repair source witness")
    else:
        raise ExportError("Unknown export field")


def _validate_snapshot(data):
    if type(data) is not dict or type(data.get("format_version")) is not int:
        raise ExportError("Unsupported snapshot format")
    version = data["format_version"]
    if version not in _TABLES_BY_FORMAT:
        raise ExportError("Unsupported snapshot format")
    if data.keys() != _DATA_KEYS_BY_FORMAT[version]:
        raise ExportError("Snapshot has missing or unknown members")
    workspace = data["workspace"]
    workspace_fields = {"id", "currency", "schema_epoch", "mode"}
    if version == 2:
        workspace_fields |= {"projection_epoch", "current_freeze_id"}
    if type(workspace) is not dict or workspace.keys() != workspace_fields:
        raise ExportError("Invalid workspace row")
    for field, value in workspace.items():
        _validate_field(field, value)
    if data["schema_versions"] != list(SCHEMA_VERSIONS_BY_FORMAT[version]):
        raise ExportError("Unsupported database schema: explicit reader update required")
    if type(data["snapshot_witness"]) is not str or not re.fullmatch(
        r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", data["snapshot_witness"]
    ):
        raise ExportError("Invalid snapshot witness")
    if type(data["server_version"]) is not str or not re.fullmatch(
        r"[0-9]{5,8}", data["server_version"]
    ):
        raise ExportError("Invalid server version")
    for table in _TABLES_BY_FORMAT[version]:
        rows = data[table]
        if type(rows) is not list:
            raise ExportError("Snapshot table must be an array")
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise ExportLimitExceeded("Table row limit exceeded")
        previous = None
        for row in rows:
            if type(row) is not dict or row.keys() != _ROW_KEYS[table]:
                raise ExportError("Snapshot row has missing or unknown fields")
            if row["workspace_id"] != workspace["id"]:
                raise ExportError("Cross-workspace snapshot row")
            for field, value in row.items():
                _validate_field(field, value)
            identity = tuple(row[field] for field in _SORT_KEYS[table])
            if previous is not None and previous >= identity:
                raise ExportError("Rows must have unique sorted identities")
            previous = identity
    encoded = _json_bytes(data)
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ExportLimitExceeded("Snapshot byte limit exceeded")
    return encoded


def capture_snapshot(conn, workspace_id: str) -> dict:
    """Materialize one bounded workspace snapshot, using no production posting helper."""
    _uuid(workspace_id)
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ExportError("Export requires an idle autocommit connection")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return read_snapshot(cur, workspace_id)


def read_snapshot(cur, workspace_id: str) -> dict:
    """Read raw rows in the caller's transaction through a dict_row cursor.

    No transaction or isolation changes are made here. The caller must provide a
    single coherent snapshot or hold the exclusive workspace barrier throughout.
    Maintenance uses this under its READ WRITE transaction after draining writers.
    """
    _uuid(workspace_id)
    workspace = cur.execute(
        "SELECT id,currency,schema_epoch,mode FROM ledger.workspace WHERE id=%s",
        (workspace_id,),
    ).fetchone()
    if workspace is None:
        raise ExportError("Workspace not found")
    schemas = cur.execute("SELECT version FROM ledger.schema_migration ORDER BY version").fetchall()
    versions = [row["version"] for row in schemas]
    version = next(
        (v for v, expected in SCHEMA_VERSIONS_BY_FORMAT.items() if versions == list(expected)), None
    )
    if version is None:
        raise ExportError("Unsupported database schema: explicit reader update required")
    if version == 2:
        workspace = cur.execute(
            "SELECT id,currency,schema_epoch,mode,projection_epoch,current_freeze_id "
            "FROM ledger.workspace WHERE id=%s",
            (workspace_id,),
        ).fetchone()
    witness = cur.execute(
        "SELECT pg_current_snapshot()::text AS snapshot_witness, "
        "current_setting('server_version_num') AS server_version"
    ).fetchone()
    result = {
        "format_version": version,
        "workspace": _normal_row(workspace),
        "schema_versions": versions,
        **witness,
    }
    for name, (table, fields, ordering) in _TABLES_BY_FORMAT[version].items():
        if name in {"operations", "requests"}:
            # Bound the two unbounded bytea/jsonb fields before transferring
            # full rows. Hexadecimal encoding doubles the payload bytes.
            size = cur.execute(
                "SELECT coalesce(sum(2::numeric * octet_length(payload) "
                "+ 2::numeric * octet_length(payload_sha256) "
                "+ octet_length(response_body::text)), 0) AS estimated_bytes "
                f"FROM ledger.{table} WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()["estimated_bytes"]
            if size > MAX_BUNDLE_BYTES:
                raise ExportLimitExceeded("Payload transfer byte limit exceeded")
        elif name == "repairs":
            size = cur.execute(
                "SELECT coalesce(sum(octet_length(source_witness)), 0) AS estimated_bytes "
                "FROM ledger.projection_repair WHERE workspace_id=%s",
                (workspace_id,),
            ).fetchone()["estimated_bytes"]
            if size > MAX_BUNDLE_BYTES:
                raise ExportLimitExceeded("Repair witness transfer byte limit exceeded")
        # Every interpolated identifier/clause is a module constant, never input.
        rows = cur.execute(
            f"SELECT {fields} FROM ledger.{table} WHERE workspace_id=%s "
            f"ORDER BY {ordering} LIMIT %s",
            (workspace_id, MAX_ROWS_PER_TABLE + 1),
        ).fetchall()
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise ExportLimitExceeded("Table row limit exceeded")
        result[name] = [_normal_row(row) for row in rows]
    _validate_snapshot(result)
    return result


def _write_file(path, contents):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(contents)
        file.flush()
        os.fsync(file.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _row_counts(data):
    return {
        "workspace": 1,
        **{table: len(data[table]) for table in _TABLES_BY_FORMAT[data["format_version"]]},
    }


def export_workspace(
    conn, workspace_id: str, output_root: Path, *, code_revision: str, dirty: bool = False
) -> Path:
    """Capture a consistent snapshot and publish its complete local bundle."""
    data = capture_snapshot(conn, workspace_id)
    return publish_snapshot(data, output_root, code_revision=code_revision, dirty=dirty)


def publish_snapshot(data, output_root: Path, *, code_revision: str, dirty: bool = False) -> Path:
    """Publish a fresh immutable UUID bundle from already materialized raw rows.

    Failed publication may leave this call's recognizable temporary directory. If
    the final parent fsync fails after rename, a valid final bundle may already
    exist; callers must inspect that UUID rather than infer that publication failed.
    """
    if type(code_revision) is not str or not re.fullmatch(r"[0-9a-f]{40}", code_revision):
        raise ExportError("Code revision must be a full Git commit hash")
    if type(dirty) is not bool:
        raise ExportError("Dirty flag must be boolean")
    encoded = _validate_snapshot(data)
    export_id = str(uuid4())
    manifest = {
        "format_version": data["format_version"],
        "export_id": export_id,
        "workspace_id": data["workspace"]["id"],
        "code_revision": code_revision,
        "dirty": dirty,
        "data_bytes": len(encoded),
        "data_sha256": hashlib.sha256(encoded).hexdigest(),
        "row_counts": _row_counts(data),
    }
    manifest_bytes = _json_bytes(manifest)
    if len(encoded) + len(manifest_bytes) > MAX_BUNDLE_BYTES:
        raise ExportLimitExceeded("Complete bundle byte limit exceeded")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise ExportError("Export root must be a regular directory")
    temporary = output_root / f".ledgerops-export-{export_id}.tmp"
    final = output_root / export_id
    if final.exists() or final.is_symlink():
        raise ExportError("Export UUID already exists")
    temporary.mkdir(mode=0o700)
    _write_file(temporary / "data.json", encoded)
    _write_file(temporary / "manifest.json", manifest_bytes)
    _sync_directory(temporary)
    # Validate the actual written files before making them visible as complete.
    _load_directory(temporary, expected_id=export_id)
    if final.exists() or final.is_symlink():
        raise ExportError("Export UUID already exists")
    os.rename(temporary, final)
    _sync_directory(output_root)
    return final


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("Duplicate JSON object key")
        result[key] = value
    return result


def _read_json(directory_fd, name, maximum):
    # Nonblocking open also lets us reject a FIFO/device before any read can wait.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(descriptor, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ExportError("Bundle members must be regular files")
        raw = file.read(maximum + 1)
        if len(raw) > maximum:
            raise ExportLimitExceeded("Bundle member byte limit exceeded")
    try:

        def reject_constant(_):
            raise ExportError("Non-finite JSON value")

        parsed = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ExportError("Invalid bundle JSON") from exc
    return raw, parsed


def _load_directory(path, *, expected_id):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if set(os.listdir(descriptor)) != {"manifest.json", "data.json"}:
            raise ExportError("Bundle has missing or unexpected files")
        manifest_raw, manifest = _read_json(descriptor, "manifest.json", MAX_MANIFEST_BYTES)
        if type(manifest) is not dict or manifest.keys() != _MANIFEST_KEYS:
            raise ExportError("Manifest has missing or unknown fields")
        if (
            type(manifest["format_version"]) is not int
            or manifest["format_version"] not in _TABLES_BY_FORMAT
        ):
            raise ExportError("Unsupported manifest format")
        _uuid(manifest["export_id"])
        _uuid(manifest["workspace_id"])
        if manifest["export_id"] != expected_id:
            raise ExportError("Export UUID does not match directory")
        if type(manifest["code_revision"]) is not str or not re.fullmatch(
            r"[0-9a-f]{40}", manifest["code_revision"]
        ):
            raise ExportError("Invalid code revision")
        if type(manifest["dirty"]) is not bool:
            raise ExportError("Invalid dirty flag")
        if (
            type(manifest["data_bytes"]) is not int
            or not 0 < manifest["data_bytes"] <= MAX_BUNDLE_BYTES
        ):
            raise ExportLimitExceeded("Invalid declared bundle size")
        if type(manifest["data_sha256"]) is not str or not re.fullmatch(
            r"[0-9a-f]{64}", manifest["data_sha256"]
        ):
            raise ExportError("Invalid data digest")
        raw, data = _read_json(descriptor, "data.json", MAX_BUNDLE_BYTES)
        if len(raw) + len(manifest_raw) > MAX_BUNDLE_BYTES:
            raise ExportLimitExceeded("Complete bundle byte limit exceeded")
        if (
            len(raw) != manifest["data_bytes"]
            or hashlib.sha256(raw).hexdigest() != manifest["data_sha256"]
        ):
            raise ExportError("Bundle data size/digest mismatch")
        _validate_snapshot(data)
        if data["format_version"] != manifest["format_version"]:
            raise ExportError("Manifest format does not match snapshot")
        if data["workspace"]["id"] != manifest["workspace_id"]:
            raise ExportError("Manifest workspace does not match snapshot")
        counts = manifest["row_counts"]
        if (
            type(counts) is not dict
            or counts.keys() != {"workspace", *_TABLES_BY_FORMAT[data["format_version"]]}
            or any(
                type(value) is not int or not 0 <= value <= MAX_ROWS_PER_TABLE
                for value in counts.values()
            )
            or counts != _row_counts(data)
        ):
            raise ExportError("Declared row counts do not match snapshot")
        return data
    finally:
        os.close(descriptor)


def load_export(path: Path) -> dict:
    """Load only a completed, bounded, exact-version bundle; never follows member symlinks."""
    path = Path(path)
    _uuid(path.name)  # A temporary directory can never be a completed export identity.
    try:
        return _load_directory(path, expected_id=path.name)
    except OSError as exc:
        raise ExportError("Cannot read complete export bundle") from exc
