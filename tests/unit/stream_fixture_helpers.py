"""Literal format10 fixture writer independent of the PostgreSQL producer."""

import copy
import hashlib
from uuid import uuid4

from ledgerops import stream_verification as stream
from ledgerops.export import _json_bytes
from ledgerops.stream_contract import SCHEMAS, SORT_KEYS, TABLES


def source(data):
    data = copy.deepcopy(data)
    data.update(format_version=9, schema_versions=list(SCHEMAS), server_version="180006")
    data["workspace"].setdefault("projection_epoch", 1)
    data["workspace"].setdefault("current_freeze_id", None)
    for table in TABLES:
        data.setdefault(table, [])
    return data


def write(data, path):
    data = source(data)
    path = path / str(uuid4())
    path.mkdir(parents=True)
    counts = {"workspace": 1}
    chunks = []
    for table in TABLES:
        rows = sorted(data[table], key=lambda row: tuple(row[key] for key in SORT_KEYS[table]))
        counts[table] = len(rows)
        if not rows:
            continue
        parts = []
        current = []
        size = 0
        for row in rows:
            raw = _json_bytes(row)
            if current and size + len(raw) > stream.MAX_CHUNK:
                parts.append(current)
                current = []
                size = 0
            current.append((row, raw))
            size += len(raw)
        if current:
            parts.append(current)
        for ordinal, part in enumerate(parts):
            raw = b"".join(pair[1] for pair in part)
            file = f"{table}.{ordinal:06d}.jsonl"
            (path / file).write_bytes(raw)
            chunks.append(
                dict(
                    table=table,
                    ordinal=ordinal,
                    file=file,
                    rows=len(part),
                    bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    first_key=[part[0][0][key] for key in SORT_KEYS[table]],
                    last_key=[part[-1][0][key] for key in SORT_KEYS[table]],
                )
            )
    manifest = dict(
        format_version=10,
        row_format_version=9,
        export_id=path.name,
        workspace_id=data["workspace"]["id"],
        code_revision="a" * 40,
        dirty=False,
        source={
            key: data[key]
            for key in ("workspace", "schema_versions", "snapshot_witness", "server_version")
        },
        row_counts=counts,
        total_rows=sum(counts.values()),
        total_bytes=0,
        chunks=chunks,
    )
    body_size = sum(chunk["bytes"] for chunk in chunks)
    while True:
        raw = _json_bytes(manifest)
        size = body_size + len(raw)
        if size == manifest["total_bytes"]:
            break
        manifest["total_bytes"] = size
    (path / "manifest.json").write_bytes(raw)
    return path
