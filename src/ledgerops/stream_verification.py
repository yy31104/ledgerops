"""Disk-backed independent financial verification for stream format10.

All source rows are retained in a bounded scratch SQLite index. Journal/account
folds and request checks stream. Other independent v9 checks run on complete,
byte-bounded semantic closures. A closure beyond the declared budget is explicitly
unverified and cannot produce healthy=true. No posting helper or database driver
is imported by this consumer.
"""

import argparse
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Direct execution also works from a directory containing only these five
# independent stdlib modules; no installed LedgerOps package is required.
if __package__ in (None, ""):
    import types

    package = types.ModuleType("ledgerops")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules.setdefault("ledgerops", package)

from ledgerops import verification as audit
from ledgerops.stream_contract import (
    INT_FIELDS,
    MONEY_FIELDS,
    NULLABLE_FIELDS,
    ROW_FIELDS,
    SCHEMAS,
    SORT_KEYS,
    TABLE_NULLABLE_FIELDS,
    TABLES,
    UUID_FIELDS,
)

MAX_BYTES = 256 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_CHUNK = 1024 * 1024
MAX_ROW = 4 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_COHORT = 16 * 1024 * 1024
MAX_ISSUES = 1000


class StreamError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise StreamError(code)


def _json(raw):
    def reject_constant(_):
        raise ValueError("nonfinite")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=audit._unique_json_object,
            parse_constant=reject_constant,
        )
        expected = (
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            + "\n"
        ).encode()
        _require(expected == raw, "NONCANONICAL_JSON")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, StreamError):
            raise
        raise StreamError("INVALID_JSON") from exc


def _fields(value, expected):
    _require(type(value) is dict and value.keys() == set(expected), "INVALID_FIELDS")


def _hash(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "INVALID_HASH")


def _read(parent, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit, "INVALID_FILE_SIZE")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        _require(len(raw) == info.st_size, "CHANGING_FILE")
        return raw
    finally:
        os.close(descriptor)


def _validate_row(table, row, workspace):
    _fields(row, ROW_FIELDS[table])
    _require(row["workspace_id"] == workspace, "ROW_WORKSPACE_MISMATCH")
    nullable = set(NULLABLE_FIELDS) | set(TABLE_NULLABLE_FIELDS.get(table, ()))
    if table == "transport_attempts":
        nullable.add("http_status")
    for name, value in row.items():
        if value is None:
            _require(name in nullable, "INVALID_NULL")
        elif name in UUID_FIELDS:
            audit._uuid(value)
        elif name in MONEY_FIELDS:
            audit._money(value)
        elif name in INT_FIELDS:
            audit._integer(value, *INT_FIELDS[name])
        elif name.endswith("_sha256"):
            _hash(value)
        elif name.endswith("_hex"):
            _require(
                type(value) is str and len(value) % 2 == 0 and re.fullmatch(r"[0-9a-f]*", value),
                "INVALID_HEX",
            )
        elif name.endswith("_at") or name in {"dispatch_deadline", "next_due_at"}:
            audit._time(value)
        elif name in {"response_body", "resume_report"}:
            _require(type(value) is dict, "INVALID_JSON_OBJECT")
        elif name == "enabled":
            _require(type(value) is bool, "INVALID_BOOLEAN")
        else:
            _require(
                type(value) is str or name == "http_status" and type(value) is int,
                "INVALID_SCALAR_TYPE",
            )


class Store:
    def __init__(self, conn, manifest, manifest_raw):
        self.conn, self.manifest, self.manifest_raw = conn, manifest, manifest_raw
        self.source = manifest["source"]
        self.workspace = manifest["workspace_id"]
        self.content_sha256 = None

    def rows(self, table, where="", params=()):
        _require(table in TABLES, "INVALID_TABLE")
        query = (
            f'SELECT body FROM "{table}"'
            + (" WHERE " + where if where else "")
            + " ORDER BY ordinal"
        )
        for (raw,) in self.conn.execute(query, params):
            yield json.loads(raw)

    def one(self, table, column, value):
        _require(column in ROW_FIELDS[table], "INVALID_COLUMN")
        raw = self.conn.execute(
            f'SELECT body FROM "{table}" WHERE "{column}"=?', (str(value),)
        ).fetchone()
        return None if raw is None else json.loads(raw[0])

    def collect(self, table, where="", params=(), budget=MAX_COHORT):
        query = f'SELECT coalesce(sum(length(body)),0) FROM "{table}"' + (
            " WHERE " + where if where else ""
        )
        size = self.conn.execute(query, params).fetchone()[0]
        if size > budget:
            raise StreamError("SEMANTIC_CLOSURE_BUDGET")
        return list(self.rows(table, where, params)), size


def _manifest(raw):
    value = _json(raw)
    _fields(
        value,
        (
            "format_version row_format_version export_id workspace_id "
            "code_revision dirty source row_counts total_rows total_bytes chunks"
        ).split(),
    )
    _require(
        type(value["format_version"]) is int
        and value["format_version"] == 10
        and type(value["row_format_version"]) is int
        and value["row_format_version"] == 9,
        "UNSUPPORTED_FORMAT",
    )
    audit._uuid(value["export_id"])
    audit._uuid(value["workspace_id"])
    _require(
        type(value["code_revision"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", value["code_revision"])
        and type(value["dirty"]) is bool,
        "INVALID_CODE_VERSION",
    )
    source = value["source"]
    source_fields = "workspace schema_versions snapshot_witness server_version".split()
    if "capture_context" in source:
        source_fields.append("capture_context")
        _require(
            source["capture_context"] == "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1",
            "UNSUPPORTED_CAPTURE_CONTEXT",
        )
    _fields(source, source_fields)
    _require(source["schema_versions"] == list(SCHEMAS), "UNSUPPORTED_SCHEMA")
    workspace = source["workspace"]
    _fields(workspace, "id currency schema_epoch mode projection_epoch current_freeze_id".split())
    _require(
        workspace["id"] == value["workspace_id"]
        and workspace["currency"] == "PLN"
        and workspace["mode"] in {"NORMAL", "FROZEN", "RESTORE"},
        "INVALID_WORKSPACE",
    )
    audit._integer(workspace["schema_epoch"], 1)
    audit._integer(workspace["projection_epoch"], 1)
    if workspace["current_freeze_id"] is not None:
        audit._uuid(workspace["current_freeze_id"])
    _require(
        type(source["snapshot_witness"]) is str
        and re.fullmatch(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", source["snapshot_witness"])
        and type(source["server_version"]) is str
        and re.fullmatch(r"[0-9]{5,8}", source["server_version"]),
        "INVALID_SNAPSHOT_METADATA",
    )
    _fields(value["row_counts"], ("workspace", *TABLES))
    for count in value["row_counts"].values():
        audit._integer(count, 0, MAX_ROWS)
    _require(
        value["row_counts"]["workspace"] == 1
        and type(value["total_rows"]) is int
        and value["total_rows"] == sum(value["row_counts"].values()) <= MAX_ROWS,
        "ROW_COUNT_LIMIT",
    )
    audit._integer(value["total_bytes"], len(raw), MAX_BYTES)
    _require(
        type(value["chunks"]) is list and len(value["chunks"]) <= MAX_ROWS, "INVALID_CHUNK_LIST"
    )
    return value


@contextmanager
def stage_stream(path, *, scratch_root=None):
    """Validate every byte/row and yield an indexed scratch view; never truncates."""
    descriptor = None
    body_error = False
    try:
        path = Path(path)
        _require(not path.is_symlink(), "SYMLINK_DIRECTORY")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        raw = _read(descriptor, "manifest.json", MAX_MANIFEST)
        manifest = _manifest(raw)
        try:
            audit._uuid(path.name)
        except (ValueError, TypeError) as exc:
            raise StreamError("COMPLETED_EXPORT_DIRECTORY_REQUIRED") from exc
        _require(path.name == manifest["export_id"], "COMPLETED_EXPORT_DIRECTORY_REQUIRED")
        chunks = manifest["chunks"]
        expected_files = {"manifest.json"}
        counts = {name: 0 for name in TABLES}
        next_ordinal = {name: 0 for name in TABLES}
        last_keys = {}
        previous_table = -1
        total_bytes = len(raw)
        content = hashlib.sha256()
        with tempfile.TemporaryDirectory(prefix="ledgerops-stream-", dir=scratch_root) as directory:
            conn = sqlite3.connect(Path(directory) / "index.sqlite3")
            try:
                conn.execute("PRAGMA page_size=4096")
                conn.execute("PRAGMA max_page_count=131072")
                conn.execute("PRAGMA cache_size=-2048")
                conn.execute("PRAGMA journal_mode=OFF")
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("PRAGMA temp_store=FILE")
                conn.execute("PRAGMA mmap_size=0")
                store = Store(conn, manifest, raw)
                for table in TABLES:
                    columns = ",".join(f'"{name}" TEXT' for name in ROW_FIELDS[table])
                    keys = ",".join(f'"{name}"' for name in SORT_KEYS[table])
                    conn.execute(
                        f'CREATE TABLE "{table}" (ordinal INTEGER,body TEXT,{columns},'
                        f"PRIMARY KEY({keys}))"
                    )
                    for name in ROW_FIELDS[table]:
                        if name.endswith("_id") or name in {
                            "provider_namespace",
                            "source_sequence",
                            "event_id",
                            "class",
                            "owner_id",
                        }:
                            conn.execute(f'CREATE INDEX "ix_{table}_{name}" ON "{table}"("{name}")')
                for chunk in chunks:
                    _fields(
                        chunk, "table ordinal file rows bytes sha256 first_key last_key".split()
                    )
                    table = chunk["table"]
                    _require(
                        table in TABLES and TABLES.index(table) >= previous_table,
                        "INVALID_TABLE_ORDER",
                    )
                    previous_table = TABLES.index(table)
                    ordinal = chunk["ordinal"]
                    _require(
                        type(ordinal) is int and ordinal == next_ordinal[table], "CHUNK_ORDINAL_GAP"
                    )
                    next_ordinal[table] += 1
                    filename = f"{table}.{ordinal:06d}.jsonl"
                    _require(
                        chunk["file"] == filename and filename not in expected_files,
                        "INVALID_CHUNK_FILE",
                    )
                    expected_files.add(filename)
                    audit._integer(chunk["rows"], 1, MAX_ROWS)
                    audit._integer(chunk["bytes"], 1, MAX_ROW)
                    _hash(chunk["sha256"])
                    _require(chunk["bytes"] <= MAX_CHUNK or chunk["rows"] == 1, "CHUNK_BYTE_LIMIT")
                    payload = _read(descriptor, filename, MAX_ROW)
                    total_bytes += len(payload)
                    _require(
                        total_bytes <= MAX_BYTES
                        and len(payload) == chunk["bytes"]
                        and hashlib.sha256(payload).hexdigest() == chunk["sha256"],
                        "CHUNK_HASH_OR_SIZE_MISMATCH",
                    )
                    content.update(payload)
                    first = last = None
                    observed = 0
                    for line in payload.splitlines(keepends=True):
                        row = _json(line)
                        _validate_row(table, row, manifest["workspace_id"])
                        key = tuple(row[name] for name in SORT_KEYS[table])
                        _require(
                            table not in last_keys or key > last_keys[table],
                            "UNSORTED_OR_DUPLICATE_ROW",
                        )
                        first = key if first is None else first
                        last = last_keys[table] = key
                        observed += 1
                        counts[table] += 1
                        _require(1 + sum(counts.values()) <= MAX_ROWS, "ROW_COUNT_LIMIT")
                        names = ROW_FIELDS[table]
                        columns = ",".join(f'"{name}"' for name in names)
                        values = [json.dumps(row, sort_keys=True, separators=(",", ":"))] + [
                            None if row[name] is None else str(row[name]) for name in names
                        ]
                        placeholders = ",".join("?" for _ in range(len(names) + 2))
                        conn.execute(
                            f'INSERT INTO "{table}"(ordinal,body,{columns}) VALUES({placeholders})',
                            (counts[table] - 1, *values),
                        )
                    _require(
                        observed == chunk["rows"]
                        and audit._same_json(list(first), chunk["first_key"])
                        and audit._same_json(list(last), chunk["last_key"]),
                        "CHUNK_KEY_OR_COUNT_MISMATCH",
                    )
                _require(set(os.listdir(descriptor)) == expected_files, "UNKNOWN_OR_MISSING_FILES")
                _require(
                    total_bytes == manifest["total_bytes"]
                    and counts
                    == {k: v for k, v in manifest["row_counts"].items() if k != "workspace"},
                    "TOTAL_COVERAGE_MISMATCH",
                )
                conn.commit()
                store.content_sha256 = content.hexdigest()
                try:
                    yield store
                except BaseException:
                    body_error = True
                    raise
            finally:
                conn.close()
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, RecursionError) as exc:
        if body_error or isinstance(exc, StreamError):
            raise
        if (
            isinstance(exc, OSError)
            and exc.errno in {errno.ENOSPC, errno.EDQUOT}
            or isinstance(exc, sqlite3.Error)
            and getattr(exc, "sqlite_errorcode", 0) & 0xFF
            in {sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM}
        ):
            raise StreamError("SCRATCH_RESOURCE_LIMIT") from exc
        raise StreamError("INVALID_STREAM_ARTIFACT") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class Check(audit._Check):
    def __init__(self):
        super().__init__()
        self.unsupported = []
        self.issue_count = 0
        self.unsupported_count = 0

    def issue(self, section, code, object_id, field, expected, actual):
        self.flags[section] = False
        self.issue_count += 1
        if len(self.issues) < MAX_ISSUES:

            def bounded(value):
                encoded = json.dumps(value, sort_keys=True, default=str)
                if len(encoded) <= 512:
                    return value
                return {
                    "truncated": True,
                    "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                    "preview": encoded[:512],
                }

            self.issues.append(
                audit.VerificationIssue(
                    code, str(object_id), field, bounded(expected), bounded(actual)
                )
            )

    def rows(self, snapshot, table, workspace):
        rows = snapshot.get(table)
        if type(rows) is not list:
            self.issue("structure", "INVALID_TABLE", table, table, "complete closure", None)
            return []
        for row in rows:
            if type(row) is not dict or row.get("workspace_id") != workspace:
                self.issue("structure", "INVALID_ROW", table, table, "same workspace", None)
        return rows

    def missing(self, scope, identifier):
        self.unsupported_count += 1
        if len(self.unsupported) < MAX_ISSUES:
            self.unsupported.append(
                {
                    "scope": scope,
                    "object_id": identifier,
                    "reason": "SEMANTIC_CLOSURE_EXCEEDS_16_MIB",
                }
            )
        self.issue(
            "structure",
            "UNVERIFIED_SEMANTIC_CLOSURE",
            identifier,
            scope,
            "within declared closure budget",
            None,
        )


def _query_count(store, query, params=()):
    return store.conn.execute(query, params).fetchone()[0]


def _foreign_keys(store, check):
    links = {
        "balances": [("account_id", "accounts", "id")],
        "journals": [("operation_id", "operations", "operation_id")],
        "entries": [("journal_id", "journals", "journal_id"), ("account_id", "accounts", "id")],
        "requests": [("operation_id", "operations", "operation_id")],
        "reversal_attempts": [
            ("operation_id", "operations", "operation_id"),
            ("original_operation_id", "operations", "operation_id"),
        ],
        "reversal_effects": [
            ("original_operation_id", "operations", "operation_id"),
            ("reversal_operation_id", "operations", "operation_id"),
        ],
        "payouts": [
            ("operation_id", "operations", "operation_id"),
            ("merchant_account_id", "accounts", "id"),
        ],
        "reservations": [
            ("payout_id", "payouts", "operation_id"),
            ("account_id", "accounts", "id"),
        ],
        "reservation_events": [("payout_id", "payouts", "operation_id")],
        "dispatch_intents": [("payout_id", "payouts", "operation_id")],
        "transport_attempts": [("payout_id", "payouts", "operation_id")],
        "provider_observations": [("payout_id", "payouts", "operation_id")],
        "observation_receipts": [
            ("payout_id", "payouts", "operation_id"),
            ("observation_id", "provider_observations", "observation_id"),
            ("transport_attempt_id", "transport_attempts", "attempt_id"),
        ],
        "observation_quarantines": [("payout_id", "payouts", "operation_id")],
        "payout_incidents": [
            ("payout_id", "payouts", "operation_id"),
            ("observation_id", "provider_observations", "observation_id"),
        ],
        "payout_resolutions": [
            ("payout_id", "payouts", "operation_id"),
            ("observation_id", "provider_observations", "observation_id"),
            ("journal_id", "journals", "journal_id"),
        ],
        "related_incidents": [
            ("payout_id", "payouts", "operation_id"),
            ("observation_id", "provider_observations", "observation_id"),
        ],
        "operator_actions": [
            ("payout_id", "payouts", "operation_id"),
            ("observation_id", "provider_observations", "observation_id"),
        ],
        "operator_approvals": [("action_id", "operator_actions", "action_id")],
        "operator_executions": [
            ("action_id", "operator_actions", "action_id"),
            ("action_id", "operator_approvals", "action_id"),
        ],
        "payout_incident_resolutions": [
            ("incident_id", "payout_incidents", "incident_id"),
            ("payout_id", "payouts", "operation_id"),
            ("action_id", "operator_executions", "action_id"),
            ("observation_id", "provider_observations", "observation_id"),
        ],
    }
    for table, relations in links.items():
        for column, parent, key in relations:
            count = _query_count(
                store,
                f'SELECT count(*) FROM "{table}" c LEFT JOIN "{parent}" p '
                f'ON c."{column}"=p."{key}" WHERE c."{column}" IS NOT NULL '
                f'AND p."{key}" IS NULL',
            )
            if count:
                check.issue(
                    "structure",
                    "MISSING_REFERENCE",
                    table,
                    column,
                    "all referenced rows present",
                    count,
                )
    # A reference to an existing operation is insufficient. Otherwise a child
    # attached to another kind never enters the kind-specific semantic closure.
    for table, column, kind, admission in (
        ("payouts", "operation_id", "PAYOUT", "ACCEPTED"),
        ("reversal_attempts", "operation_id", "REVERSAL", None),
        ("reversal_attempts", "original_operation_id", "PURCHASE", "ACCEPTED"),
        ("reversal_effects", "original_operation_id", "PURCHASE", "ACCEPTED"),
        ("reversal_effects", "reversal_operation_id", "REVERSAL", "ACCEPTED"),
    ):
        count = _query_count(
            store,
            f'SELECT count(*) FROM "{table}" c JOIN operations o '
            f'ON c."{column}"=o.operation_id WHERE o.kind<>?'
            + (" OR o.admission<>?" if admission is not None else ""),
            (kind, admission) if admission is not None else (kind,),
        )
        if count:
            check.issue("structure", "INVALID_OPERATION_KIND", table, column, kind, count)
    for query, code in [
        (
            (
                "SELECT count(*) FROM (SELECT class,owner_id FROM accounts GROUP BY "
                "class,owner_id HAVING count(*)>1)"
            ),
            "DUPLICATE_OWNER_CLASS",
        ),
        (
            (
                "SELECT count(*) FROM (SELECT operation_id FROM journals GROUP BY "
                "operation_id HAVING count(*)>1)"
            ),
            "DUPLICATE_EFFECT",
        ),
        (
            (
                "SELECT count(*) FROM (SELECT journal_id,account_id FROM entries "
                "GROUP BY journal_id,account_id HAVING count(*)>1)"
            ),
            "DUPLICATE_JOURNAL_ACCOUNT",
        ),
    ]:
        count = _query_count(store, query)
        if count:
            check.issue("structure", code, "snapshot", "uniqueness", "unique", count)


def _closure(store, operation):
    """Complete operation dependencies, with a measured byte bound before decode."""
    op_ids = {operation["operation_id"]}
    originals = set()
    selected = {name: [] for name in TABLES}
    sizes = {}
    used = 0

    def collect(table, where="", params=()):
        nonlocal used
        previous = sizes.get(table, 0)
        rows, size = store.collect(table, where, params, budget=MAX_COHORT - used + previous)
        used += size - previous
        sizes[table] = size
        selected[table] = rows
        return rows

    if operation["kind"] == "REVERSAL":
        # Retained relations remain usable dependencies when canonical B intent
        # is malformed. Never let request parsing bypass own journal structure/A.
        attempts = collect("reversal_attempts", "operation_id=?", (operation["operation_id"],))
        originals.update(row["original_operation_id"] for row in attempts)
        try:
            request = audit._reversal(
                audit._payload(operation), store.workspace, operation["operation_id"]
            )
            originals.add(request["original_operation_id"])
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            pass  # The B checker reports invalid canonical intent after structure/A.
        original_marks = ",".join("?" for _ in originals)
        where = "reversal_operation_id=?"
        params = (operation["operation_id"],)
        if originals:
            where += f" OR original_operation_id IN ({original_marks})"
            params += tuple(originals)
        effects = collect("reversal_effects", where, params)
        originals.update(row["original_operation_id"] for row in effects)
        op_ids |= originals
        op_ids.update(row["reversal_operation_id"] for row in effects)

    marks = ",".join("?" for _ in op_ids)
    operations = collect("operations", f"operation_id IN ({marks})", tuple(op_ids))
    headers = collect("journals", f"operation_id IN ({marks})", tuple(op_ids))
    journal_ids = [row["journal_id"] for row in headers]
    if journal_ids:
        collect("entries", f"journal_id IN ({','.join('?' for _ in journal_ids)})", journal_ids)
    if operation["kind"] == "REVERSAL":
        collect("reversal_attempts", f"operation_id IN ({marks})", tuple(op_ids))
    if operation["kind"] == "PAYOUT":
        identifier = operation["operation_id"]
        collect("payouts", "operation_id=?", (identifier,))
        for table in (
            "reservations",
            "reservation_events",
            "dispatch_intents",
            "transport_attempts",
            "provider_observations",
            "observation_receipts",
            "observation_quarantines",
            "payout_incidents",
            "payout_resolutions",
            "related_incidents",
            "operator_actions",
            "payout_incident_resolutions",
        ):
            collect(table, "payout_id=?", (identifier,))
        action_ids = [row["action_id"] for row in selected["operator_actions"]]
        if action_ids:
            action_marks = ",".join("?" for _ in action_ids)
            for table in ("operator_approvals", "operator_executions"):
                collect(table, f"action_id IN ({action_marks})", action_ids)
    account_ids = {row["account_id"] for row in selected["entries"]}
    account_ids.update(row["merchant_account_id"] for row in selected["payouts"])
    for row in operations:
        parser = {"PURCHASE": audit._purchase, "PAYOUT": audit._payout_request}.get(row["kind"])
        if parser is None:
            continue
        try:
            payload = parser(audit._payload(row), store.workspace, row["operation_id"])
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            continue  # Entries still supply all accounts needed for structure/A.
        account_ids.update(
            value
            for name, value in payload.items()
            if name.endswith("account_id") and type(value) is str
        )
    for row in store.rows("accounts", "class=?", ("SYNTHETIC_CLEARING",)):
        account_ids.add(row["id"])
    if account_ids:
        collect("accounts", f"id IN ({','.join('?' for _ in account_ids)})", tuple(account_ids))
    selected.update(format_version=9, **store.source)
    return selected


def _maintenance(store, check):
    try:
        snapshot = {"format_version": 9, **store.source}
        used = 0
        ids = set()
        for table in ("maintenance_freezes", "repairs", "repair_rows"):
            rows, size = store.collect(table, budget=MAX_COHORT - used)
            used += size
            snapshot[table] = rows
            if table == "repair_rows":
                ids.update(row["account_id"] for row in rows)
        accounts = {identifier: store.one("accounts", "id", identifier) for identifier in ids}
        audit._check_maintenance(check, snapshot, store.source["workspace"], accounts)
    except StreamError:
        check.missing("maintenance", "snapshot")


def _global_projection(store, check):
    extras = _query_count(
        store,
        "SELECT count(*) FROM balances b "
        "LEFT JOIN accounts a ON b.account_id=a.id WHERE a.id IS NULL",
    )
    if extras:
        check.issue(
            "projection", "EXTRA_PROJECTION", "balances", "account", "existing account", extras
        )
    for row in store.rows("accounts"):
        identifier = row["id"]
        audit._check_accounts(check, [row])
        debit = credit = held = 0
        for entry in store.rows("entries", "account_id=?", (identifier,)):
            if entry["side"] == "D":
                debit += int(entry["amount_minor"])
            elif entry["side"] == "C":
                credit += int(entry["amount_minor"])
        for reservation in store.rows("reservations", "account_id=?", (identifier,)):
            latest = store.conn.execute(
                "SELECT state FROM reservation_events WHERE payout_id=? "
                "ORDER BY CAST(event_no AS INTEGER) DESC LIMIT 1",
                (reservation["payout_id"],),
            ).fetchone()
            if latest is not None and latest[0] == "HELD":
                held += int(reservation["gross_minor"])
        balance = store.one("balances", "account_id", identifier)
        audit._check_projections(
            check,
            [] if balance is None else [balance],
            {identifier: row},
            {identifier: {"D": debit, "C": credit}},
            {identifier: held},
        )


def verify_store(store):
    check = Check()
    check.intent_scope = ("PURCHASE", "REVERSAL", "PAYOUT")
    check.unverified.update(
        {
            "PAYOUT_PROVIDER_AUTHENTICITY",
            "PAYOUT_EXTERNAL_EFFECT",
            "OPERATOR_HISTORICAL_AUTHORIZATION",
        }
    )
    _foreign_keys(store, check)
    _maintenance(store, check)
    external_consistent = True
    payout_count = resolved_count = 0
    for operation in store.rows("operations"):
        try:
            snapshot = _closure(store, operation)
            accounts = audit._check_accounts(check, snapshot["accounts"])
            operations = audit._check_operations(check, snapshot["operations"], 9)
            _, lines, _, by_operation = audit._check_journals(
                check, snapshot["journals"], snapshot["entries"], accounts, operations
            )
            audit._check_intent(check, store.workspace, accounts, operations, by_operation, lines)
            if operation["kind"] == "REVERSAL":
                audit._check_reversals(
                    check, snapshot, store.workspace, accounts, operations, by_operation, lines
                )
            if operation["kind"] == "PAYOUT":
                audit._check_payouts(
                    check, snapshot, store.workspace, accounts, operations, by_operation, lines
                )
                evidence = check.external_evidence
                external_consistent &= evidence["external_consistency_ok"]
                payout_count += evidence["accepted_payout_count"]
                resolved_count += evidence["resolved_payout_count"]
        except StreamError:
            check.missing("operation", operation["operation_id"])
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            check.issue(
                "intent",
                "INVALID_OPERATION_CLOSURE",
                operation["operation_id"],
                "closure",
                str(exc),
                None,
            )
    for request in store.rows("requests"):
        operation = store.one("operations", "operation_id", request["operation_id"])
        audit._check_requests(
            check, [request], {} if operation is None else {operation["operation_id"]: operation}
        )
    for operation in store.rows("operations"):
        if operation["kind"] != "OPENING" and not _query_count(
            store,
            "SELECT count(*) FROM requests WHERE operation_id=?",
            (operation["operation_id"],),
        ):
            check.issue(
                "intent",
                "MISSING_REQUEST",
                operation["operation_id"],
                "request_result",
                "present",
                None,
            )
    # Cross-payout event and sequence reuse cannot disappear at cohort boundaries.
    conflict = _query_count(
        store,
        (
            "SELECT count(*) FROM (SELECT provider_namespace,event_id FROM "
            "provider_observations GROUP BY provider_namespace,event_id HAVING "
            "count(DISTINCT envelope_sha256)>1)"
        ),
    )
    conflict += _query_count(
        store,
        (
            "SELECT count(*) FROM (SELECT provider_namespace,source_sequence "
            "FROM provider_observations WHERE kind='execution' GROUP BY "
            "provider_namespace,source_sequence HAVING count(DISTINCT "
            "envelope_sha256)>1)"
        ),
    )
    if conflict:
        external_consistent = False
        check.issue(
            "external",
            "PROVIDER_IDENTITY_CONFLICT",
            "snapshot",
            "provider_identity",
            "unique authenticated fact",
            conflict,
        )
    _global_projection(store, check)
    complete = not check.unsupported
    healthy = (
        complete
        and all(check.flags[name] for name in ("structure", "projection", "intent"))
        and external_consistent
    )
    result = {
        "report_version": 4,
        "format_version": 10,
        "verification_engine": "SQLITE_BOUNDED_SEMANTIC_CLOSURES_V1",
        "structure_ok": check.flags["structure"],
        "projection_ok": check.flags["projection"],
        "request_intent_ok": check.flags["intent"],
        "reservation_supported": check.flags["reservation"],
        "healthy": healthy,
        "rebuild_eligible": complete
        and check.flags["structure"]
        and check.flags["intent"]
        and check.flags["reservation"],
        "verification_complete": complete,
        "unverified_scopes": check.unsupported,
        "unverified_scope_count": check.unsupported_count,
        "issues": [issue.__dict__ for issue in check.issues],
        "issue_count": check.issue_count,
        "issues_truncated": check.issue_count > len(check.issues),
        "intent_scope": list(check.intent_scope),
        "unverified_intent_kinds": sorted(check.unverified),
        "external_evidence": {
            "external_consistency_ok": external_consistent,
            "accepted_payout_count": payout_count,
            "resolved_payout_count": resolved_count,
            "payouts_resolved": payout_count == resolved_count,
            "signature_authenticity": "NOT_VERIFIED_NO_TRUST_KEYS",
            "independent_provider_history": "NOT_PROVIDED",
        },
        "source_export_id": store.manifest["export_id"],
        "source_manifest_sha256": hashlib.sha256(store.manifest_raw).hexdigest(),
        "source_content_sha256": store.content_sha256,
        "limits": {
            "source_bytes": MAX_BYTES,
            "source_rows": MAX_ROWS,
            "scratch_sqlite_bytes": 512 * 1024 * 1024,
            "sqlite_cache_bytes": 2 * 1024 * 1024,
            "semantic_closure_bytes": MAX_COHORT,
        },
    }

    if "capture_context" in store.source:
        result["capture_context"] = store.source["capture_context"]
    return result


def verify_stream(path, *, scratch_root=None):
    try:
        with stage_stream(path, scratch_root=scratch_root) as store:
            return verify_store(store)
    except sqlite3.Error as exc:
        raise StreamError("SCRATCH_RESOURCE_LIMIT") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--scratch-root", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify_stream(args.path, scratch_root=args.scratch_root)
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0 if report["healthy"] else 1
    except (StreamError, MemoryError) as exc:
        code = "CONSUMER_MEMORY_LIMIT" if isinstance(exc, MemoryError) else str(exc)
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
