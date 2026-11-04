"""Format10 financial JSONL exports from one bounded PostgreSQL snapshot.

The unchanged financial row-format9 allowlist contains no credentials. Complete
coverage is explicit for every table, including empty tables. Chunk hashes are
integrity evidence, not proof of financial correctness or provider authenticity.
"""

import hashlib
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from ledgerops.export import (
    _ROW_KEYS_BY_FORMAT,
    _SORT_KEYS,
    _TABLES_BY_FORMAT,
    SCHEMA_VERSIONS_BY_FORMAT,
    ExportError,
    ExportLimitExceeded,
    _financial_schema_versions,
    _json_bytes,
    _normal_row,
    _sync_directory,
    _uuid,
    _validate_field,
    _write_file,
)

FORMAT_VERSION = 10
ROW_FORMAT_VERSION = 9
CHUNK_BYTES = 1024 * 1024
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_TOTAL_ROWS = 1_000_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
BATCH_ROWS = 16


def _row(row, table, workspace):
    result = _normal_row(row)
    if result.keys() != _ROW_KEYS_BY_FORMAT[ROW_FORMAT_VERSION][table]:
        raise ExportError("Stream row has missing or unknown fields")
    if result["workspace_id"] != workspace:
        raise ExportError("Cross-workspace stream row")
    for field, value in result.items():
        _validate_field(field, value, version=ROW_FORMAT_VERSION, table=table)
    encoded = _json_bytes(result)
    if len(encoded) > MAX_ROW_BYTES:
        raise ExportLimitExceeded("Stream single-row byte limit exceeded")
    return result, encoded


def _metadata(conn, workspace):
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT id,currency,schema_epoch,mode,projection_epoch,current_freeze_id "
            "FROM ledger.workspace WHERE id=%s",
            (workspace,),
        ).fetchone()
        if row is None:
            raise ExportError("Workspace not found")
        versions = _financial_schema_versions(cur)
        if versions != list(SCHEMA_VERSIONS_BY_FORMAT[ROW_FORMAT_VERSION]):
            raise ExportError("Stream requires exact row-format9 schema")
        witness = cur.execute(
            "SELECT pg_current_snapshot()::text AS snapshot_witness, "
            "current_setting('server_version_num') AS server_version"
        ).fetchone()
    row = _normal_row(row)
    for field, value in row.items():
        _validate_field(field, value, version=ROW_FORMAT_VERSION)
    return {"workspace": row, "schema_versions": versions, **witness}


def _manifest_bytes(manifest, chunk_bytes):
    # total_bytes includes its own decimal spelling and the final canonical LF.
    manifest["total_bytes"] = chunk_bytes
    while True:
        encoded = _json_bytes(manifest)
        total = chunk_bytes + len(encoded)
        if total == manifest["total_bytes"]:
            break
        manifest["total_bytes"] = total
    if len(encoded) > MAX_MANIFEST_BYTES or total > MAX_TOTAL_BYTES:
        raise ExportLimitExceeded("Stream complete-bundle byte limit exceeded")
    return encoded


def export_stream(conn, workspace_id, output_root, *, code_revision, dirty=False):
    """Publish all financial rows through named cursors; limits fail, never truncate.

    Only owned unpublished staging is removed on failure. If final-parent fsync
    fails after rename, the UUID directory may exist and must be inspected rather
    than assuming publication rolled back. Errors expose export_id for that audit.
    """
    _uuid(workspace_id)
    if type(code_revision) is not str or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None:
        raise ExportError("Full Git revision required")
    if type(dirty) is not bool:
        raise ExportError("Dirty flag must be boolean")
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ExportError("Stream requires idle autocommit reader connection")
    root = Path(output_root).absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ExportError("Stream root must be a regular directory")
    export_id = str(uuid4())
    stage = root / f".ledgerops-stream-{export_id}.tmp"
    final = root / export_id
    stage.mkdir(mode=0o700)
    chunk_bytes, total_rows, chunks = 0, 1, []
    row_counts = {"workspace": 1}
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL timezone='UTC'")
            conn.execute("SET LOCAL bytea_output='hex'")
            conn.execute("SET LOCAL statement_timeout='120s'")
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL idle_in_transaction_session_timeout='180s'")
            source = _metadata(conn, workspace_id)
            for table, (relation, fields, ordering) in _TABLES_BY_FORMAT[
                ROW_FORMAT_VERSION
            ].items():
                # All interpolated SQL comes from the fixed financial allowlist.
                # The 256-byte margin covers key suffixes and integer quoting in
                # normalization; exact serialized row size is also checked below.
                estimate = conn.execute(
                    f"SELECT coalesce(max(octet_length(row_to_json(r)::text)),0) "
                    f"FROM (SELECT {fields} FROM ledger.{relation} WHERE workspace_id=%s) r",
                    (workspace_id,),
                ).fetchone()[0]
                if estimate + 256 > MAX_ROW_BYTES:
                    raise ExportLimitExceeded("Stream source-row transfer byte limit exceeded")
                ordinal, buffered, size, first, previous = 0, [], 0, None, None
                row_counts[table] = 0

                def flush(last_key, table=table):
                    nonlocal ordinal, buffered, size, first, chunk_bytes
                    if not buffered:
                        return
                    raw = b"".join(buffered)
                    if chunk_bytes + len(raw) > MAX_TOTAL_BYTES:
                        raise ExportLimitExceeded("Stream chunk-disk byte limit exceeded")
                    filename = f"{table}.{ordinal:06d}.jsonl"
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
                    chunk_bytes += len(raw)
                    ordinal += 1
                    buffered, size, first = [], 0, None

                name = f"ledgerops_stream_{uuid4().hex}"
                with conn.cursor(name=name, row_factory=dict_row) as cursor:
                    cursor.execute(
                        f"SELECT {fields} FROM ledger.{relation} WHERE workspace_id=%s "
                        f"ORDER BY {ordering}",
                        (workspace_id,),
                    )
                    while batch := cursor.fetchmany(BATCH_ROWS):
                        for raw_row in batch:
                            row, raw = _row(raw_row, table, workspace_id)
                            key = tuple(row[field] for field in _SORT_KEYS[table])
                            if previous is not None and key <= previous:
                                raise ExportError("Stream rows require unique sorted tuple keys")
                            if total_rows >= MAX_TOTAL_ROWS:
                                raise ExportLimitExceeded("Stream total-row limit exceeded")
                            if buffered and size + len(raw) > CHUNK_BYTES:
                                flush(previous)
                            if not buffered:
                                first = key
                            buffered.append(raw)
                            size += len(raw)
                            previous = key
                            total_rows += 1
                            row_counts[table] += 1
                            if size >= CHUNK_BYTES:
                                flush(previous)
                    flush(previous)
            manifest = {
                "format_version": FORMAT_VERSION,
                "row_format_version": ROW_FORMAT_VERSION,
                "export_id": export_id,
                "workspace_id": workspace_id,
                "code_revision": code_revision,
                "dirty": dirty,
                "source": source,
                "row_counts": row_counts,
                "total_rows": total_rows,
                "total_bytes": 0,
                "chunks": chunks,
            }
            encoded = _manifest_bytes(manifest, chunk_bytes)
            _write_file(stage / "manifest.json", encoded)
        _sync_directory(stage)
        if final.exists() or final.is_symlink():
            raise ExportError("Stream UUID already exists")
        os.rename(stage, final)
        _sync_directory(root)
        return final
    except BaseException as exc:
        exc.export_id = export_id
        if stage.exists():
            shutil.rmtree(stage)
            _sync_directory(root)
        raise
