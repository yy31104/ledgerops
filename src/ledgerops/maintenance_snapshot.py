"""Complete bounded maintenance observations under the caller's exclusive barrier.

This is a READ COMMITTED write-transaction view, including its own uncommitted
projection/audit writes. It never opens a PostgreSQL transaction or changes its
isolation. Public read-only RR export remains a separate entry point.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from ledgerops.export import (
    _TABLES_BY_FORMAT,
    ExportError,
    ExportLimitExceeded,
    _json_bytes,
    _sync_directory,
    _write_file,
    publish_snapshot,
    read_snapshot,
)
from ledgerops.stream_contract import ROW_FIELDS, SORT_KEYS, TABLES
from ledgerops.stream_export import (
    BATCH_ROWS,
    CHUNK_BYTES,
    MAX_ROW_BYTES,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_ROWS,
    _manifest_bytes,
    _metadata,
    _row,
)
from ledgerops.stream_verification import Store, StreamError, _validate_row, verify_store
from ledgerops.verification import verify_snapshot

CAPTURE_CONTEXT = "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1"


@dataclass(frozen=True)
class StreamReview:
    data: dict

    @property
    def structure_ok(self):
        return self.data["structure_ok"]

    @property
    def projection_ok(self):
        return self.data["projection_ok"]

    @property
    def request_intent_ok(self):
        return self.data["request_intent_ok"]

    @property
    def reservation_supported(self):
        return self.data["reservation_supported"]

    @property
    def verification_complete(self):
        return self.data["verification_complete"]

    @property
    def rebuild_eligible(self):
        return self.data["rebuild_eligible"]

    def as_dict(self):
        return copy.deepcopy(self.data)


@dataclass
class Assessment:
    report: object
    snapshot: dict | None = None
    store: object | None = None

    def projection_input(self):
        if self.snapshot is not None:
            return self.snapshot
        if self.store.manifest["row_counts"]["accounts"] > 10000:
            raise ExportLimitExceeded("Maintenance repair account limit exceeded")
        balances, _ = self.store.collect("balances")
        return {
            "capture_kind": CAPTURE_CONTEXT,
            "format_version": 9,
            "snapshot_witness": self.store.source["snapshot_witness"],
            "balances": balances,
        }

    def publish(self, output_root, *, code_revision, dirty=False):
        if self.snapshot is not None:
            return publish_snapshot(
                self.snapshot, output_root, code_revision=code_revision, dirty=dirty
            )
        return _publish(self, output_root, code_revision=code_revision, dirty=dirty)


def _barrier(cur, workspace):
    if cur.connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ExportError("Maintenance snapshot requires an active transaction")
    state = cur.execute("SHOW transaction_isolation").fetchone()
    if state["transaction_isolation"] != "read committed":
        raise ExportError("Maintenance snapshot requires existing READ COMMITTED")
    owned = cur.execute(
        "WITH key AS (SELECT hashtextextended(%s,0) AS value) SELECT EXISTS(SELECT 1 FROM "
        "pg_locks l,key k WHERE l.pid=pg_backend_pid() AND l.locktype='advisory' "
        "AND l.mode='ExclusiveLock' AND l.granted AND l.objsubid=1 "
        "AND l.classid=((k.value>>32)&4294967295)::oid "
        "AND l.objid=(k.value&4294967295)::oid) AS owned",
        (f"ledgerops:workspace:{workspace}",),
    ).fetchone()["owned"]
    if not owned:
        raise ExportError("Maintenance snapshot requires the exclusive workspace barrier")


def _index(conn):
    conn.execute("PRAGMA page_size=4096")
    conn.execute("PRAGMA max_page_count=131072")
    conn.execute("PRAGMA cache_size=-2048")
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA mmap_size=0")
    for table in TABLES:
        columns = ",".join(f'"{name}" TEXT' for name in ROW_FIELDS[table])
        keys = ",".join(f'"{name}"' for name in SORT_KEYS[table])
        conn.execute(
            f'CREATE TABLE "{table}" (ordinal INTEGER,body TEXT,{columns},PRIMARY KEY({keys}))'
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


def _capture(cur, workspace, sqlite_conn):
    if tuple(_TABLES_BY_FORMAT[9]) != TABLES:
        raise ExportError("Explicit maintenance row-contract update required")
    source = _metadata(cur.connection, workspace)
    source["capture_context"] = CAPTURE_CONTEXT
    counts = {"workspace": 1}
    total_bytes, total_rows, content = 0, 1, hashlib.sha256()
    _index(sqlite_conn)
    for table, (relation, fields, ordering) in _TABLES_BY_FORMAT[9].items():
        estimate = cur.execute(
            f"SELECT coalesce(max(octet_length(row_to_json(r)::text)),0) AS bytes "
            f"FROM (SELECT {fields} FROM ledger.{relation} WHERE workspace_id=%s) r",
            (workspace,),
        ).fetchone()["bytes"]
        if estimate + 256 > MAX_ROW_BYTES:
            raise ExportLimitExceeded("Maintenance source-row transfer limit exceeded")
        counts[table] = 0
        previous = None
        with cur.connection.cursor(
            name=f"ledgerops_maintenance_{uuid4().hex}", row_factory=dict_row
        ) as reader:
            reader.execute(
                f"SELECT {fields} FROM ledger.{relation} WHERE workspace_id=%s ORDER BY {ordering}",
                (workspace,),
            )
            while batch := reader.fetchmany(BATCH_ROWS):
                for raw in batch:
                    row, encoded = _row(raw, table, workspace)
                    _validate_row(table, row, workspace)
                    key = tuple(row[name] for name in SORT_KEYS[table])
                    if previous is not None and key <= previous:
                        raise ExportError("Maintenance source identities must be unique and sorted")
                    previous = key
                    total_bytes += len(encoded)
                    total_rows += 1
                    if total_bytes > MAX_TOTAL_BYTES or total_rows > MAX_TOTAL_ROWS:
                        raise ExportLimitExceeded("Maintenance complete-source limit exceeded")
                    content.update(encoded)
                    names = ROW_FIELDS[table]
                    columns = ",".join(f'"{name}"' for name in names)
                    placeholders = ",".join("?" for _ in range(len(names) + 2))
                    values = [
                        json.dumps(
                            row,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                            allow_nan=False,
                        )
                    ] + [None if row[name] is None else str(row[name]) for name in names]
                    sqlite_conn.execute(
                        f'INSERT INTO "{table}"(ordinal,body,{columns}) VALUES({placeholders})',
                        (counts[table], *values),
                    )
                    counts[table] += 1
    internal = {
        "capture_version": 1,
        "capture_context": CAPTURE_CONTEXT,
        "export_id": None,
        "workspace_id": workspace,
        "source": source,
        "row_counts": counts,
        "total_rows": total_rows,
        "content_sha256": content.hexdigest(),
    }
    encoded = _json_bytes(internal)
    if total_bytes + len(encoded) > MAX_TOTAL_BYTES:
        raise ExportLimitExceeded("Maintenance complete-source limit exceeded")
    sqlite_conn.commit()
    store = Store(sqlite_conn, internal, encoded)
    store.content_sha256 = content.hexdigest()
    return store


@contextmanager
def assess_current(cur, workspace_id, *, scratch_root=None):
    _barrier(cur, workspace_id)
    try:
        snapshot = read_snapshot(cur, workspace_id)
    except ExportLimitExceeded:
        pass
    else:
        yield Assessment(verify_snapshot(snapshot), snapshot=snapshot)
        return
    with tempfile.TemporaryDirectory(
        prefix="ledgerops-maintenance-", dir=scratch_root
    ) as temporary:
        conn = sqlite3.connect(Path(temporary) / "snapshot.sqlite3")
        try:
            store = _capture(cur, workspace_id, conn)
            report = verify_store(store)
            report["source_export_id"] = None
            report["source_manifest_sha256"] = None
            report["capture_context"] = CAPTURE_CONTEXT
            yield Assessment(StreamReview(report), store=store)
        except sqlite3.Error as exc:
            raise StreamError("SCRATCH_RESOURCE_LIMIT") from exc
        finally:
            conn.close()


def _publish(assessment, output_root, *, code_revision, dirty):
    if (
        type(code_revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or type(dirty) is not bool
    ):
        raise ExportError("Full Git revision and explicit dirty flag required")
    root = Path(output_root).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ExportError("Maintenance preview root must be a regular directory")
    identity = str(uuid4())
    stage, final = root / f".ledgerops-maintenance-preview-{identity}.tmp", root / identity
    stage.mkdir(mode=0o700)
    store = assessment.store
    chunks, total_bytes = [], 0
    try:
        for table in TABLES:
            ordinal, buffered, size, first, last = 0, [], 0, None, None

            def flush(last_key, table=table):
                nonlocal ordinal, buffered, size, first, total_bytes
                if not buffered:
                    return
                raw = b"".join(buffered)
                filename = f"{table}.{ordinal:06d}.jsonl"
                if total_bytes + len(raw) > MAX_TOTAL_BYTES:
                    raise ExportLimitExceeded("Maintenance preview disk limit exceeded")
                _write_file(stage / filename, raw)
                chunks.append(
                    {
                        "table": table,
                        "ordinal": ordinal,
                        "file": filename,
                        "rows": len(buffered),
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "first_key": list(first),
                        "last_key": list(last_key),
                    }
                )
                total_bytes += len(raw)
                ordinal += 1
                buffered, size, first = [], 0, None

            for row in store.rows(table):
                raw = _json_bytes(row)
                if buffered and size + len(raw) > CHUNK_BYTES:
                    flush(last)
                key = tuple(row[name] for name in SORT_KEYS[table])
                if not buffered:
                    first = key
                buffered.append(raw)
                size += len(raw)
                last = key
                if size >= CHUNK_BYTES:
                    flush(last)
            flush(last)
        manifest = {
            "format_version": 10,
            "row_format_version": 9,
            "export_id": identity,
            "workspace_id": store.workspace,
            "code_revision": code_revision,
            "dirty": dirty,
            "source": store.source,
            "row_counts": store.manifest["row_counts"],
            "total_rows": store.manifest["total_rows"],
            "total_bytes": 0,
            "chunks": chunks,
        }
        encoded = _manifest_bytes(manifest, total_bytes)
        _write_file(stage / "manifest.json", encoded)
        _sync_directory(stage)
        if final.exists() or final.is_symlink():
            raise ExportError("Preview UUID already exists")
        os.rename(stage, final)
        _sync_directory(root)
        store.manifest, store.manifest_raw = manifest, encoded
        assessment.report = StreamReview(verify_store(store))
        return final
    except BaseException as exc:
        exc.export_id = identity
        if stage.exists():
            shutil.rmtree(stage)
            _sync_directory(root)
        raise
