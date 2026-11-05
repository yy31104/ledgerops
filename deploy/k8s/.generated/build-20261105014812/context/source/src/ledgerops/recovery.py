"""Bounded restore investigation using signed fixed-watermark provider history.

There is no reconstruction from provider events and no external payment command in
this module. The retained client key, exact business bytes and original 202 remain
mandatory. Python/DB administrators and the configured synthetic provider signer
are trusted; history capture is not a claim about a real bank's finality.
"""

import hashlib
import hmac
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from ledgerops.backup import (
    BackupError,
    _closed_runtime,
    _digest_file,
    _idle,
    _json,
    _sync,
    _write,
    canonical,
)
from ledgerops.db.payout_dispatch import _lock_work
from ledgerops.db.payout_resolution import receive_observation
from ledgerops.domain import OperationId, WorkspaceId
from ledgerops.provider_protocol import (
    InvalidObservation,
    ProviderTrust,
    _object,
    verify_observation,
)

MAX_HISTORY_EVENTS = 10000
MAX_HISTORY_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    namespace: str
    watermark: int
    events: tuple[bytes, ...]
    page_sha256: tuple[str, ...]
    pages: tuple[bytes, ...]


def _signed(envelope, trust):
    if type(envelope) is not dict or envelope.keys() != {
        "key_id",
        "payload",
        "signature",
    }:
        raise InvalidObservation("invalid history proof envelope")
    key, payload, signature = (envelope[name] for name in ("key_id", "payload", "signature"))
    if (
        type(key) is not str
        or key not in trust.keys
        or type(payload) is not str
        or type(signature) is not str
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
    ):
        raise InvalidObservation("untrusted history proof")
    expected = hmac.new(
        trust.keys[key],
        b"ledgerops.provider-event:v1\n" + key.encode() + b"\n" + payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidObservation("invalid history signature")
    return _object(payload)


def _execution(raw, trust):
    envelope = _object(raw)
    payload = _signed(envelope, trust)
    request, key = payload.get("request"), payload.get("provider_key")
    if type(request) is not dict or type(key) is not str:
        raise InvalidObservation("history requires execution requests")
    parts = key.split(".")
    try:
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != str(trust.namespace):
            raise ValueError
        if any(str(UUID(part)) != part for part in parts[1:]):
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidObservation("invalid history execution identity") from exc
    observation = verify_observation(
        raw, trust=trust, provider_key=key, request_bytes=canonical(request)
    )
    if observation.kind != "execution":
        raise InvalidObservation("history requires execution events")
    return observation, payload


def capture_history(fetch_page, *, trust, page_limit=16):
    """Verify signatures, exact page membership and every sequence through one watermark.

    fetch_page(after=..., watermark=..., limit=...) returns bounded response bytes.
    First request fixes the watermark; subsequent requests always send it, even if
    the provider accepts more work meanwhile. Missing/duplicated/reordered events
    and incomplete coverage fail before any restored financial state is touched.
    """
    if type(trust) is not ProviderTrust or type(page_limit) is not int or not 1 <= page_limit <= 32:
        raise ValueError("trusted configuration and page limit 1..32 required")
    after, watermark, size = 0, None, 0
    events, digests, pages = [], [], []
    while True:
        raw = fetch_page(after=after, watermark=watermark, limit=page_limit)
        page = _object(raw)
        size += len(raw)
        if size > MAX_HISTORY_BYTES:
            raise InvalidObservation("history byte limit")
        if page.keys() != {
            "namespace",
            "watermark",
            "next_after",
            "complete",
            "events",
            "proof",
        }:
            raise InvalidObservation("invalid history page fields")
        if watermark is None:
            watermark = page["watermark"]
        end = page["next_after"]
        items = page["events"]
        if (
            page["namespace"] != str(trust.namespace)
            or type(watermark) is not int
            or not 0 <= watermark <= 2**63 - 1
            or type(page["watermark"]) is not int
            or page["watermark"] != watermark
            or type(end) is not int
            or not after <= end <= watermark
            or type(page["complete"]) is not bool
            or page["complete"] != (end == watermark)
            or type(items) is not list
            or len(items) > page_limit
            or end != after + len(items)
            or (not items and after != watermark)
        ):
            raise InvalidObservation("history coverage mismatch")
        proof = _signed(page["proof"], trust)
        try:
            expected = {
                "kind": "history_page",
                "namespace": str(trust.namespace),
                "watermark": watermark,
                "after": after,
                "next_after": end,
                "complete": page["complete"],
                "event_payload_hashes": [
                    hashlib.sha256(item["payload"].encode()).hexdigest() for item in items
                ],
            }
        except (KeyError, TypeError, AttributeError) as exc:
            raise InvalidObservation("invalid history member") from exc
        if canonical(proof) != canonical(expected):
            raise InvalidObservation("history page proof mismatch")
        for index, item in enumerate(items, start=after + 1):
            event = canonical(item)
            observation, _ = _execution(event, trust)
            if observation.source_sequence != index:
                raise InvalidObservation("missing or reordered history sequence")
            events.append(event)
        if len(events) > MAX_HISTORY_EVENTS:
            raise InvalidObservation("history event limit")
        digests.append(hashlib.sha256(raw).hexdigest())
        pages.append(raw)
        if page["complete"]:
            return HistorySnapshot(
                str(trust.namespace),
                watermark,
                tuple(events),
                tuple(digests),
                tuple(pages),
            )
        after = end


def save_history(history, destination):
    """Retain exact signed pages in a private, newly published observation artifact."""
    if type(history) is not HistorySnapshot:
        raise TypeError("verified history snapshot required")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise BackupError("NEW_HISTORY_DESTINATION_REQUIRED")
    raw = canonical(
        {
            "history_version": 1,
            "namespace": history.namespace,
            "watermark": history.watermark,
            "pages": [page.decode("utf-8") for page in history.pages],
        }
    )
    if len(raw) > 3 * MAX_HISTORY_BYTES:
        raise BackupError("HISTORY_ARTIFACT_SIZE_LIMIT")
    stage = destination.parent / f".ledgerops-history-{uuid4()}.tmp"
    try:
        _write(stage, raw)
        # Link creates a new name atomically and cannot replace an existing artifact.
        os.link(stage, destination)
        _sync(destination.parent)
    finally:
        stage.unlink(missing_ok=True)
        _sync(destination.parent)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def load_history(path, *, trust):
    """Authenticate persisted page bytes again; artifact metadata is not authority."""
    path = Path(path)
    _digest_file(path, maximum=3 * MAX_HISTORY_BYTES)
    artifact = _json(path.read_bytes())
    if (
        type(artifact) is not dict
        or artifact.keys() != {"history_version", "namespace", "watermark", "pages"}
        or type(artifact["history_version"]) is not int
        or artifact["history_version"] != 1
        or type(artifact["pages"]) is not list
        or not 1 <= len(artifact["pages"]) <= MAX_HISTORY_EVENTS + 1
        or any(type(page) is not str for page in artifact["pages"])
    ):
        raise InvalidObservation("invalid retained history artifact")
    pages = iter(artifact["pages"])
    try:
        history = capture_history(
            lambda **_: next(pages).encode("utf-8"), trust=trust, page_limit=32
        )
    except StopIteration as exc:
        raise InvalidObservation("incomplete retained history artifact") from exc
    if (
        next(pages, None) is not None
        or artifact["namespace"] != history.namespace
        or type(artifact["watermark"]) is not int
        or artifact["watermark"] != history.watermark
    ):
        raise InvalidObservation("retained history artifact binding mismatch")
    return history


def reconcile_restored_history(conn, history, *, trust, lookup):
    """Admin-controlled HISTORY/LOOKUP convergence, always leaving runtime CONNECT closed.

    Provider calls complete before temporarily entering NORMAL for the established
    resolver. Session workspace barriers exclude cooperating administrators. A kill
    during this narrow window may leave mode NORMAL, but CONNECT remains closed;
    normal service/worker startup still needs its independent external authorization.
    No freeze record or current_freeze_id is erased. Handled exits on a usable
    connection restore RESTORE; a lost administrator connection needs inspection.
    """
    _idle(conn)
    if conn.execute(
        "SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' LIMIT 1"
    ).fetchone():
        raise BackupError("DEDICATED_RESTORE_CONNECTION_REQUIRED")
    if (
        type(history) is not HistorySnapshot
        or type(trust) is not ProviderTrust
        or history.namespace != str(trust.namespace)
        or not callable(lookup)
        or re.fullmatch(r"ledgerops_restore_[a-z0-9_]{8,40}", conn.info.dbname) is None
    ):
        raise BackupError("INVALID_RESTORE_HISTORY_CONTEXT")
    pages = iter(history.pages)
    try:
        verified = capture_history(lambda **_: next(pages), trust=trust, page_limit=32)
    except StopIteration as exc:
        raise InvalidObservation("incomplete retained history") from exc
    if next(pages, None) is not None or verified != history:
        raise InvalidObservation("retained history snapshot mismatch")
    _closed_runtime(conn)
    started = time.monotonic()
    workspaces = conn.execute(
        "SELECT id,mode FROM ledger.workspace ORDER BY id LIMIT 10001"
    ).fetchall()
    if len(workspaces) > 10000 or any(row[1] != "RESTORE" for row in workspaces):
        raise BackupError("RESTORE_MODE_REQUIRED")
    for workspace, _ in workspaces:
        if (
            conn.execute(
                "SELECT 1 FROM ledger.auth_restore_workspace WHERE workspace_id=%s",
                (workspace,),
            ).fetchone()
            is None
        ):
            raise BackupError("RESTORE_ACCESS_INVALIDATION_REQUIRED")
    rows = conn.execute(
        "SELECT workspace_id,operation_id,provider_key,provider_request FROM ledger.payout "
        "WHERE provider_namespace=%s ORDER BY workspace_id,operation_id LIMIT 10001",
        (history.namespace,),
    ).fetchall()
    if len(rows) > 10000:
        raise BackupError("RESTORE_PAYOUT_LIMIT")
    retained = {row[2]: row for row in rows}
    by_key, gaps = {}, []
    incomplete = conn.execute(
        "SELECT p.provider_key FROM ledger.payout p LEFT JOIN ledger.operation o "
        "ON o.workspace_id=p.workspace_id AND o.operation_id=p.operation_id "
        "WHERE p.provider_namespace=%s AND (o.operation_id IS NULL OR o.kind<>'PAYOUT' "
        "OR o.admission<>'ACCEPTED' OR o.http_status<>202 OR NOT EXISTS (SELECT 1 FROM "
        "ledger.request_result r WHERE r.workspace_id=p.workspace_id AND "
        "r.operation_id=p.operation_id AND r.route_family='payout:v1' AND r.http_status=202 "
        "AND r.payload=o.payload AND r.payload_sha256=o.payload_sha256 AND "
        "r.response_body=o.response_body)) ORDER BY p.provider_key",
        (history.namespace,),
    ).fetchall()
    for row in incomplete:
        gaps.append({"provider_key": row[0], "code": "MISSING_ADMISSION_KEY_AND_202"})
    # Reverify every event against this trust configuration, never trust caller-made payloads.
    for raw in history.events:
        _, payload = _execution(raw, trust)
        key = payload["provider_key"]
        local = retained.get(key)
        if local is None:
            gaps.append({"provider_key": key, "code": "MISSING_ADMISSION_KEY_AND_202"})
        else:
            verify_observation(raw, trust=trust, provider_key=key, request_bytes=local[3])
            by_key.setdefault(key, []).append(raw)
    base = {
        "provider_namespace": history.namespace,
        "watermark": history.watermark,
        "history_events": len(history.events),
        "history_pages": len(history.page_sha256),
        "page_sha256": list(history.page_sha256),
        "external_puts": 0,
        "runtime_connect_enabled": False,
        "external_dispatch_enabled": False,
    }
    if gaps:
        return {
            **base,
            "status": "RESTORE_GAP",
            "gaps": gaps,
            "observed_recovery_seconds": time.monotonic() - started,
        }
    # Network I/O is outside financial transactions; LOOKUP never dispatches or closes a key.
    lookups = {}
    for key in retained:
        if time.monotonic() - started > 600:
            raise BackupError("RESTORE_INVESTIGATION_DEADLINE")
        lookups[key] = lookup(key)
    for key, raw in lookups.items():
        verify_observation(raw, trust=trust, provider_key=key, request_bytes=retained[key][3])
    workspaces = sorted({row[0] for row in rows})
    locked, activated, results = [], [], []
    try:
        for workspace in workspaces:
            locked.append(workspace)
            with conn.transaction():
                conn.execute("SET LOCAL statement_timeout='10s'")
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s,0))",
                    (f"ledgerops:workspace:{workspace}",),
                )
        _closed_runtime(conn)
        for workspace in workspaces:
            if conn.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() != ("RESTORE",):
                raise BackupError("RESTORE_MODE_REQUIRED")
            if (
                conn.execute(
                    "SELECT 1 FROM ledger.auth_restore_workspace WHERE workspace_id=%s",
                    (workspace,),
                ).fetchone()
                is None
            ):
                raise BackupError("RESTORE_ACCESS_INVALIDATION_REQUIRED")
        # Verify exact retained business intent and original receipt before any financial effect.
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            for row in rows:
                _lock_work(cur, row[0], row[1])
        for workspace in workspaces:
            activated.append(workspace)
            conn.execute("UPDATE ledger.workspace SET mode='NORMAL' WHERE id=%s", (workspace,))
        for key, row in retained.items():
            if time.monotonic() - started > 600:
                raise BackupError("RESTORE_INVESTIGATION_DEADLINE")
            for channel, raw in [("HISTORY", event) for event in by_key.get(key, [])] + [
                ("LOOKUP", lookups[key])
            ]:
                result = receive_observation(
                    conn,
                    WorkspaceId(row[0]),
                    OperationId(row[1]),
                    raw,
                    trust=trust,
                    channel=channel,
                )
                results.append(
                    {
                        "provider_key": key,
                        "channel": channel,
                        "status": result.status,
                        "state": result.state,
                        "code": result.code,
                    }
                )
        state = (
            "RECOVERED"
            if all(row["status"] in {"APPLIED", "REPLAYED", "OBSERVED"} for row in results)
            else "BLOCKED"
        )
        if (
            state == "RECOVERED"
            and conn.execute(
                "SELECT 1 FROM ledger.payout p WHERE provider_namespace=%s AND NOT EXISTS "
                "(SELECT 1 FROM ledger.payout_resolution r WHERE r.workspace_id=p.workspace_id "
                "AND r.payout_id=p.operation_id) LIMIT 1",
                (history.namespace,),
            ).fetchone()
        ):
            state = "UNRESOLVED"
        return {
            **base,
            "status": state,
            "gaps": [],
            "results": results,
            "observed_recovery_seconds": time.monotonic() - started,
        }
    finally:
        try:
            if not conn.closed:
                for workspace in activated:
                    conn.execute(
                        "UPDATE ledger.workspace SET mode='RESTORE' WHERE id=%s",
                        (workspace,),
                    )
                _closed_runtime(conn)
        finally:
            try:
                for workspace in reversed(locked):
                    if not conn.closed:
                        conn.execute(
                            "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                            (f"ledgerops:workspace:{workspace}",),
                        )
            except BaseException:
                conn.close()  # Failed unlock must not return a live session barrier.
                raise
