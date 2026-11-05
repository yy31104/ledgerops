"""Immutable local reconciliation evidence, without financial write authority.

The registry transaction serializes promotion and durable references; it does not
make PostgreSQL and the filesystem one atomic storage engine. A failed promotion
may leave an unreferenced object. No automatic garbage collection is implemented.
"""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

MAX_OBJECT_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PHYSICAL_ID = re.compile(r"[0-9a-f]{64}:[a-z_]+:(?:0|[1-9][0-9]*)")


class PublicationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise PublicationError("INVALID_JSON") from exc


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _uuid(value):
    try:
        parsed = UUID(str(value))
        if str(value) != str(parsed):
            raise ValueError("noncanonical UUID")
        return parsed
    except (ValueError, AttributeError) as exc:
        raise PublicationError("INVALID_IDENTITY") from exc


def _hash(value):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise PublicationError("INVALID_DIGEST")
    return value


def _strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("nonfinite number")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise PublicationError("INVALID_JSON") from exc


@dataclass(frozen=True, slots=True)
class ObjectRef:
    sha256: str
    bytes: int
    pin_id: UUID


@dataclass(frozen=True, slots=True)
class Completion:
    run_id: UUID
    result_sha256: str
    semantic_sha256: str
    published: bool
    evidence_epoch: int | None


@dataclass(frozen=True, slots=True)
class PinnedRun:
    workspace_id: UUID
    run_id: UUID
    reader_id: UUID
    result_sha256: str
    semantic_sha256: str
    manifest: dict
    partition_plan: dict
    result: dict
    published_epoch: int | None
    is_current_at_pin: bool
    evidence_epoch_at_pin: int


class ObjectStore:
    """Content-addressed files in a dedicated local directory; no cleanup policy."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        missing, parent = [], self.root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise PublicationError("INVALID_OBJECT_ROOT")
        self._sync_directory(self.root)
        # Persist newly created directory entries, including a fresh storage root.
        for path in reversed(missing):
            self._sync_directory(path.parent)

    def _directory(self, workspace, *, create=False):
        workspace = _uuid(workspace)
        path = self.root / str(workspace)
        if create:
            path.mkdir(exist_ok=True, mode=0o700)
            self._sync_directory(self.root)
        if path.is_symlink() or not path.is_dir():
            raise PublicationError("OBJECT_STORE_UNAVAILABLE")
        return path

    @staticmethod
    def _sync_directory(directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stage(self, workspace, raw):
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_OBJECT_BYTES:
            raise PublicationError("OBJECT_SIZE_LIMIT")
        directory = self._directory(workspace, create=True)
        name = directory / f".recon-{uuid4()}.tmp"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
            self._sync_directory(directory)
        except BaseException:
            # This call owns this exact temporary file. No final/unknown file is deleted.
            name.unlink(missing_ok=True)
            raise
        return name, _digest(raw), len(raw)

    def _promote(self, workspace, staged, digest, size):
        """Called only while the database registry guard is held."""
        directory = self._directory(workspace)
        final = directory / _hash(digest)
        if final.exists() or final.is_symlink():
            self.read(workspace, digest, size)
            staged.unlink()
        else:
            os.rename(staged, final)
        self._sync_directory(directory)
        self.read(workspace, digest, size)

    def read(self, workspace, digest, size):
        _hash(digest)
        if type(size) is not int or not 1 <= size <= MAX_OBJECT_BYTES:
            raise PublicationError("OBJECT_SIZE_LIMIT")
        path = self._directory(workspace) / digest
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                raise PublicationError("CORRUPT_OBJECT")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(size + 1)
            if len(raw) != size or _digest(raw) != digest:
                raise PublicationError("CORRUPT_OBJECT")
            return raw
        except FileNotFoundError as exc:
            raise PublicationError("MISSING_OBJECT") from exc
        except OSError as exc:
            raise PublicationError("OBJECT_STORE_UNAVAILABLE") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def _transaction(conn):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise PublicationError("IDLE_AUTOCOMMIT_REQUIRED")
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        yield cur


def _registry(cur, workspace, *, create=False):
    if create:
        cur.execute(
            "INSERT INTO ledger.recon_registry_guard(workspace_id) VALUES(%s) "
            "ON CONFLICT(workspace_id) DO NOTHING",
            (workspace,),
        )
    guard = cur.execute(
        "SELECT evidence_epoch FROM ledger.recon_registry_guard WHERE workspace_id=%s FOR UPDATE",
        (workspace,),
    ).fetchone()
    if guard is None:
        raise PublicationError("UNKNOWN_REGISTRY")
    return guard["evidence_epoch"]


def _run(cur, workspace, run_id):
    result = cur.execute(
        "SELECT * FROM ledger.recon_run WHERE workspace_id=%s AND run_id=%s FOR UPDATE",
        (workspace, run_id),
    ).fetchone()
    if result is None:
        raise PublicationError("UNKNOWN_RUN")
    return result


def _pin(cur, workspace, digest, owner_type, owner_id, run_id=None):
    pin_id = uuid4()
    cur.execute(
        "INSERT INTO "
        "ledger.evidence_pin(workspace_id,pin_id,object_sha256,owner_type,owner_id,run_id) "
        "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,object_sha256,owner_type,owner_id) "
        "DO NOTHING",
        (workspace, pin_id, digest, owner_type, owner_id, run_id),
    )
    row = cur.execute(
        "SELECT pin_id,run_id FROM ledger.evidence_pin WHERE workspace_id=%s AND object_sha256=%s "
        "AND owner_type=%s AND owner_id=%s",
        (workspace, digest, owner_type, owner_id),
    ).fetchone()
    if row["run_id"] != run_id:
        raise PublicationError("PIN_OWNER_IDENTITY_CONFLICT")
    return row["pin_id"]


def _register(cur, store, workspace, raw, publisher_id, staged=None):
    staged, digest, size = store._stage(workspace, raw) if staged is None else staged
    store._promote(workspace, staged, digest, size)
    cur.execute(
        "INSERT INTO ledger.recon_object(workspace_id,sha256,byte_length) VALUES(%s,%s,%s) "
        "ON CONFLICT(workspace_id,sha256) DO NOTHING",
        (workspace, digest, size),
    )
    row = cur.execute(
        "SELECT byte_length FROM ledger.recon_object WHERE workspace_id=%s AND sha256=%s",
        (workspace, digest),
    ).fetchone()
    if row["byte_length"] != size:
        raise PublicationError("OBJECT_REGISTRY_CONFLICT")
    pin = _pin(cur, workspace, digest, "PUBLISHER", publisher_id)
    return ObjectRef(digest, size, pin)


def register_object(conn, workspace_id, store, raw, *, publisher_id):
    """Promote and register with a durable publisher pin before returning a reference."""
    workspace, publisher = _uuid(workspace_id), _uuid(publisher_id)
    staged = store._stage(workspace, raw)
    try:
        with _transaction(conn) as cur:
            _registry(cur, workspace, create=True)
            return _register(cur, store, workspace, raw, publisher, staged)
    finally:
        staged[0].unlink(missing_ok=True)


def _registered_bytes(cur, store, workspace, digest):
    row = cur.execute(
        "SELECT byte_length FROM ledger.recon_object WHERE workspace_id=%s AND sha256=%s",
        (workspace, _hash(digest)),
    ).fetchone()
    if row is None:
        raise PublicationError("UNREGISTERED_OBJECT")
    return store.read(workspace, digest, row["byte_length"])


def _publisher_pin(cur, workspace, pin_id, digest):
    row = cur.execute(
        "SELECT object_sha256,owner_type FROM ledger.evidence_pin WHERE workspace_id=%s AND "
        "pin_id=%s",
        (workspace, _uuid(pin_id)),
    ).fetchone()
    if row is None or (row["object_sha256"], row["owner_type"]) != (digest, "PUBLISHER"):
        raise PublicationError("PUBLISHER_PIN_REQUIRED")


def _transfer_pin(cur, workspace, pin_id, digest, run_id):
    _publisher_pin(cur, workspace, pin_id, digest)
    _pin(cur, workspace, digest, "RUN", run_id, run_id)
    cur.execute(
        "DELETE FROM ledger.evidence_pin WHERE workspace_id=%s AND pin_id=%s",
        (workspace, pin_id),
    )


def _input_refs(manifest):
    try:
        names = ("ledger_manifest", "ledger_data", "statement")
        kind, version = manifest.get("source_kind"), manifest.get("manifest_version")
        if (
            kind not in (None, "FINANCIAL_STREAM10_COMPACT_V1")
            or type(version) is not int
            or version != (1 if kind is None else 2)
        ):
            raise ValueError("unsupported source kind/version")
        if kind == "FINANCIAL_STREAM10_COMPACT_V1":
            chunks = manifest["source_chunks"]
            if manifest.get("manifest_version") != 2 or type(chunks) is not list:
                raise ValueError("invalid stream input manifest")
            extra = []
            total = 0
            for ordinal, chunk in enumerate(chunks):
                name = f"stream_chunk_{ordinal:06d}"
                if (
                    chunk["name"] != name
                    or type(chunk["bytes"]) is not int
                    or not 1 <= chunk["bytes"] <= 4194304
                ):
                    raise ValueError("invalid stream chunk reference")
                if manifest["inputs"][name] != {"sha256": chunk["sha256"], "bytes": chunk["bytes"]}:
                    raise ValueError("stream chunk binding mismatch")
                extra.append(name)
                total += chunk["bytes"]
            names = (*names, *extra)
            if set(manifest["inputs"]) != set(names) or total > 268435456:
                raise ValueError("stream input coverage mismatch")
        refs = {name: manifest["inputs"][name] for name in names}
        for ref in refs.values():
            if type(ref) is not dict or set(ref) != {"sha256", "bytes"}:
                raise ValueError("invalid input reference")
            _hash(ref["sha256"])
            if type(ref["bytes"]) is not int or not 1 <= ref["bytes"] <= MAX_OBJECT_BYTES:
                raise ValueError("invalid input size")
        return refs
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationError("INVALID_INPUT_MANIFEST") from exc


def _validate_plan(plan, refs):
    try:
        if (
            set(plan) != {"plan_version", "units", "partitions", "expected_coverage"}
            or type(plan["plan_version"]) is not int
            or plan["plan_version"] != 1
        ):
            raise ValueError("unknown plan")
        sources = {ref["sha256"] for ref in refs.values()}
        expected = plan["expected_coverage"]
        if type(expected) is not list or len(expected) > 250000:
            raise ValueError("coverage limit")
        if expected != sorted(set(expected)) or any(
            type(row) is not str
            or _PHYSICAL_ID.fullmatch(row) is None
            or row.split(":", 1)[0] not in sources
            for row in expected
        ):
            raise ValueError("invalid physical row identity")
        units = {}
        all_rows, group_keys = [], set()
        for unit in plan["units"]:
            identifier = _hash(unit["unit_id"])
            if identifier in units or unit["coverage"] != sorted(set(unit["coverage"])):
                raise ValueError("duplicate unit")
            units[identifier] = unit
            all_rows.extend(unit["coverage"])
            for group in unit["group_keys"]:
                key = _canonical(group)
                if key in group_keys:
                    raise ValueError("settlement group split across units")
                group_keys.add(key)
        if sorted(all_rows) != expected:
            raise ValueError("physical row omission or overlap")
        partitions = plan["partitions"]
        if type(partitions) is not list or not 1 <= len(partitions) <= 64:
            raise ValueError("partition limit")
        assigned = []
        for index, part in enumerate(partitions):
            if (
                set(part) != {"partition_id", "unit_ids", "coverage"}
                or type(part["partition_id"]) is not int
                or part["partition_id"] != index
            ):
                raise ValueError("invalid partition identity")
            if part["unit_ids"] != sorted(set(part["unit_ids"])):
                raise ValueError("duplicate partition unit")
            assigned.extend(part["unit_ids"])
            covered = sorted(
                row for identifier in part["unit_ids"] for row in units[identifier]["coverage"]
            )
            if part["coverage"] != covered:
                raise ValueError("partition splits a complete unit")
        if sorted(assigned) != sorted(units):
            raise ValueError("unit omitted or assigned more than once")
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationError("INVALID_PARTITION_PLAN") from exc


def _prepared(prepared):
    from ledgerops.reconciliation import PreparedInputs

    if type(prepared) is not PreparedInputs:
        raise PublicationError("PREPARED_INPUTS_REQUIRED")
    try:
        manifest, plan = prepared.input_manifest, prepared.partition_plan
        _uuid(manifest["workspace_id"])
        _uuid(manifest["provider_namespace"])
        if date.fromisoformat(manifest["business_date"]).isoformat() != manifest["business_date"]:
            raise ValueError("invalid business date")
        if manifest["timezone"] != "Europe/Warsaw":
            raise ValueError("unsupported timezone")
        if re.fullmatch(r"[0-9a-f]{40}", manifest["code_revision"]) is None:
            raise ValueError("code revision must be an immutable commit")
        refs = _input_refs(manifest)
        _validate_plan(plan, refs)
        raw_manifest, raw_plan = _canonical(manifest), _canonical(plan)
        if len(raw_manifest) > 4194304 or len(raw_plan) > 16777216:
            raise PublicationError("RUN_METADATA_LIMIT")
        return manifest, plan, refs, raw_manifest, raw_plan
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PublicationError("INVALID_PREPARED_INPUTS") from exc


def _bind_prepared(run, prepared):
    manifest, plan, refs, raw_manifest, raw_plan = _prepared(prepared)
    if raw_manifest != run["manifest"] or raw_plan != run["partition_plan"]:
        raise PublicationError("RUN_INPUT_BINDING_MISMATCH")
    return manifest, plan, refs


def begin_run(conn, store, prepared, *, run_id, input_pins):
    """Fix source bytes and the complete group plan in one STAGING transaction."""
    manifest, plan, refs, raw_manifest, raw_plan = _prepared(prepared)
    if type(input_pins) is not dict or input_pins.keys() != refs.keys():
        raise PublicationError("INPUT_PUBLISHER_PIN_MISMATCH")
    workspace, run_id = _uuid(manifest["workspace_id"]), _uuid(run_id)
    with _transaction(conn) as cur:
        epoch = _registry(cur, workspace, create=True)
        existing = cur.execute(
            "SELECT * FROM ledger.recon_run WHERE workspace_id=%s AND run_id=%s FOR UPDATE",
            (workspace, run_id),
        ).fetchone()
        if existing is not None:
            _bind_prepared(existing, prepared)
            for name, ref in refs.items():
                supplied = input_pins[name]
                if type(supplied) is not ObjectRef or (supplied.sha256, supplied.bytes) != (
                    ref["sha256"],
                    ref["bytes"],
                ):
                    raise PublicationError("INPUT_PUBLISHER_PIN_MISMATCH")
                retained = cur.execute(
                    "SELECT 1 FROM ledger.evidence_pin WHERE workspace_id=%s "
                    "AND object_sha256=%s AND owner_type='RUN' AND owner_id=%s AND run_id=%s",
                    (workspace, supplied.sha256, run_id, run_id),
                ).fetchone()
                if retained is None:
                    raise PublicationError("RUN_REFERENCE_MISSING")
                _registered_bytes(cur, store, workspace, supplied.sha256)
                # Unknown-COMMIT recovery may reuse an already consumed pin or
                # register the same input with a new pin. Both refer to the
                # durable RUN reference; only this redundant publisher pin goes.
                cur.execute(
                    "DELETE FROM ledger.evidence_pin WHERE workspace_id=%s AND pin_id=%s "
                    "AND object_sha256=%s AND owner_type='PUBLISHER'",
                    (workspace, _uuid(supplied.pin_id), supplied.sha256),
                )
            return run_id
        for name, ref in refs.items():
            supplied = input_pins.get(name)
            if type(supplied) is not ObjectRef or (supplied.sha256, supplied.bytes) != (
                ref["sha256"],
                ref["bytes"],
            ):
                raise PublicationError("INPUT_PUBLISHER_PIN_MISMATCH")
            _publisher_pin(cur, workspace, supplied.pin_id, supplied.sha256)
            _registered_bytes(cur, store, workspace, supplied.sha256)
        current = cur.execute(
            "SELECT run_id FROM ledger.recon_current WHERE workspace_id=%s AND "
            "provider_namespace=%s "
            "AND business_date=%s",
            (workspace, manifest["provider_namespace"], manifest["business_date"]),
        ).fetchone()
        cur.execute(
            "INSERT INTO ledger.recon_run(workspace_id,run_id,provider_namespace,business_date,"
            "manifest,partition_plan,partition_count,started_epoch,expected_current) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                workspace,
                run_id,
                manifest["provider_namespace"],
                manifest["business_date"],
                raw_manifest,
                raw_plan,
                len(plan["partitions"]),
                epoch,
                None if current is None else current["run_id"],
            ),
        )
        for part in plan["partitions"]:
            cur.execute(
                "INSERT INTO ledger.recon_partition(workspace_id,run_id,partition_id,declaration) "
                "VALUES(%s,%s,%s,%s)",
                (workspace, run_id, part["partition_id"], _canonical(part)),
            )
        for supplied in input_pins.values():
            _transfer_pin(cur, workspace, supplied.pin_id, supplied.sha256, run_id)
    return run_id


def partition_envelope(prepared, run_id, partition_result):
    """Add immutable publication identity without changing semantic result bytes."""
    from ledgerops.reconciliation import semantic_digest

    _, plan, _, raw_manifest, raw_plan = _prepared(prepared)
    part_id = partition_result["partition_id"]
    if type(part_id) is not int or not 0 <= part_id < len(plan["partitions"]):
        raise PublicationError("UNKNOWN_PARTITION")
    if partition_result["coverage"] != plan["partitions"][part_id]["coverage"]:
        raise PublicationError("PARTITION_COVERAGE_MISMATCH")
    return {
        "run_id": str(_uuid(run_id)),
        "partition_id": part_id,
        "generation": 1,
        "input_manifest_sha256": _digest(raw_manifest),
        "partition_plan_sha256": _digest(raw_plan),
        "coverage": partition_result["coverage"],
        "semantic_sha256": semantic_digest(partition_result["result"]),
        "result": partition_result["result"],
    }


def _validate_envelope(envelope, run, declaration):
    from ledgerops.reconciliation import semantic_digest

    expected_keys = {
        "run_id",
        "partition_id",
        "generation",
        "input_manifest_sha256",
        "partition_plan_sha256",
        "coverage",
        "semantic_sha256",
        "result",
    }
    if type(envelope) is not dict or set(envelope) != expected_keys:
        raise PublicationError("INVALID_PARTITION_ENVELOPE")
    if (
        envelope["run_id"] != str(run["run_id"])
        or type(envelope["partition_id"]) is not int
        or envelope["partition_id"] != declaration["partition_id"]
        or type(envelope["generation"]) is not int
        or envelope["generation"] != 1
        or envelope["input_manifest_sha256"] != run["manifest_sha256"]
        or envelope["partition_plan_sha256"] != run["plan_sha256"]
    ):
        raise PublicationError("PARTITION_BINDING_MISMATCH")
    if envelope["coverage"] != declaration["coverage"]:
        raise PublicationError("PARTITION_COVERAGE_MISMATCH")
    if envelope["semantic_sha256"] != semantic_digest(envelope["result"]):
        raise PublicationError("PARTITION_SEMANTIC_DIGEST_MISMATCH")


def accept_partition(conn, workspace_id, store, *, run_id, partition_id, result_ref):
    """Accept a registered result only under the authoritative run/partition locks."""
    workspace, run_id = _uuid(workspace_id), _uuid(run_id)
    if type(result_ref) is not ObjectRef or type(partition_id) is not int:
        raise PublicationError("INVALID_ACCEPTANCE_ARGUMENTS")
    with _transaction(conn) as cur:
        _registry(cur, workspace)
        run = _run(cur, workspace, run_id)
        part = cur.execute(
            "SELECT declaration FROM ledger.recon_partition WHERE workspace_id=%s AND run_id=%s "
            "AND partition_id=%s FOR UPDATE",
            (workspace, run_id, partition_id),
        ).fetchone()
        if part is None:
            raise PublicationError("UNKNOWN_PARTITION")
        raw = _registered_bytes(cur, store, workspace, result_ref.sha256)
        if len(raw) != result_ref.bytes:
            raise PublicationError("OBJECT_LENGTH_MISMATCH")
        envelope = _strict_json(raw)
        _validate_envelope(envelope, run, _strict_json(part["declaration"]))
        accepted = cur.execute(
            "SELECT result_sha256,semantic_sha256 FROM ledger.recon_accepted_result "
            "WHERE workspace_id=%s AND run_id=%s AND partition_id=%s",
            (workspace, run_id, partition_id),
        ).fetchone()
        if accepted is not None:
            if accepted["result_sha256"] != result_ref.sha256:
                raise PublicationError("ACCEPTED_RESULT_CONFLICT")
            # A retry may have registered the same bytes with a new publisher
            # pin after losing its acknowledgement. The permanent RUN pin is
            # already durable; release only this exact redundant publisher pin.
            cur.execute(
                "DELETE FROM ledger.evidence_pin WHERE workspace_id=%s AND pin_id=%s "
                "AND object_sha256=%s AND owner_type='PUBLISHER'",
                (workspace, result_ref.pin_id, result_ref.sha256),
            )
            # Exact acknowledgement replay never creates a result or revives a failed run.
            return "ALREADY_ACCEPTED"
        if run["state"] != "STAGING":
            raise PublicationError("RUN_NOT_STAGING")
        _publisher_pin(cur, workspace, result_ref.pin_id, result_ref.sha256)
        cur.execute(
            "INSERT INTO ledger.recon_accepted_result(workspace_id,run_id,partition_id,generation,"
            "manifest_sha256,plan_sha256,result_sha256,semantic_sha256) "
            "VALUES(%s,%s,%s,1,%s,%s,%s,%s)",
            (
                workspace,
                run_id,
                partition_id,
                run["manifest_sha256"],
                run["plan_sha256"],
                result_ref.sha256,
                envelope["semantic_sha256"],
            ),
        )
        _transfer_pin(cur, workspace, result_ref.pin_id, result_ref.sha256, run_id)
        return "ACCEPTED"


def _completion(run):
    return Completion(
        run["run_id"],
        run["result_sha256"],
        run["semantic_sha256"],
        run["published"],
        run["published_epoch"],
    )


def complete_run(conn, workspace_id, store, prepared, *, run_id):
    """Publish one complete immutable report; a lost CAS preserves it without making it current."""
    from ledgerops.reconciliation import merge_partition_results, semantic_digest

    workspace, run_id = _uuid(workspace_id), _uuid(run_id)
    with _transaction(conn) as cur:
        epoch = _registry(cur, workspace)
        run = _run(cur, workspace, run_id)
        manifest, plan, refs = _bind_prepared(run, prepared)
        if run["state"] == "FAILED":
            raise PublicationError("RUN_NOT_STAGING")
        for ref in refs.values():
            _registered_bytes(cur, store, workspace, ref["sha256"])
        if run["state"] == "COMPLETE":
            references = cur.execute(
                "SELECT object_sha256 FROM ledger.evidence_pin WHERE workspace_id=%s "
                "AND owner_type='RUN' AND run_id=%s",
                (workspace, run_id),
            ).fetchall()
            for reference in references:
                _registered_bytes(cur, store, workspace, reference["object_sha256"])
            if run["result_sha256"] not in {row["object_sha256"] for row in references}:
                raise PublicationError("RUN_REFERENCE_MISSING")
            return _completion(run)
        parts = cur.execute(
            "SELECT partition_id,declaration FROM ledger.recon_partition WHERE workspace_id=%s "
            "AND run_id=%s ORDER BY partition_id FOR UPDATE",
            (workspace, run_id),
        ).fetchall()
        results = []
        for part in parts:
            accepted = cur.execute(
                "SELECT result_sha256,semantic_sha256 FROM ledger.recon_accepted_result "
                "WHERE workspace_id=%s AND run_id=%s AND partition_id=%s",
                (workspace, run_id, part["partition_id"]),
            ).fetchone()
            if accepted is None:
                raise PublicationError("INCOMPLETE_PARTITIONS")
            envelope = _strict_json(
                _registered_bytes(cur, store, workspace, accepted["result_sha256"])
            )
            declaration = _strict_json(part["declaration"])
            if declaration != plan["partitions"][part["partition_id"]]:
                raise PublicationError("PARTITION_DECLARATION_MISMATCH")
            _validate_envelope(envelope, run, declaration)
            if envelope["semantic_sha256"] != accepted["semantic_sha256"]:
                raise PublicationError("ACCEPTANCE_DIGEST_MISMATCH")
            results.append(
                {
                    "partition_id": part["partition_id"],
                    "coverage": envelope["coverage"],
                    "result": envelope["result"],
                }
            )
        if len(parts) != len(plan["partitions"]):
            raise PublicationError("INCOMPLETE_PARTITIONS")
        try:
            result = merge_partition_results(prepared, results)
        except (ValueError, TypeError, KeyError) as exc:
            raise PublicationError("INVALID_PARTITION_RESULTS") from exc
        semantic = semantic_digest(result)
        artifact = {
            "run_id": str(run_id),
            "input_manifest_sha256": run["manifest_sha256"],
            "partition_plan_sha256": run["plan_sha256"],
            "semantic_sha256": semantic,
            "result": result,
        }
        final_ref = _register(cur, store, workspace, _canonical(artifact), run_id)
        _transfer_pin(cur, workspace, final_ref.pin_id, final_ref.sha256, run_id)
        current = cur.execute(
            "SELECT run_id FROM ledger.recon_current WHERE workspace_id=%s AND "
            "provider_namespace=%s "
            "AND business_date=%s",
            (workspace, manifest["provider_namespace"], manifest["business_date"]),
        ).fetchone()
        current_id = None if current is None else current["run_id"]
        published = current_id == run["expected_current"]
        if published and epoch >= 2**63 - 1:
            raise PublicationError("EVIDENCE_EPOCH_CAPACITY")
        published_epoch = epoch + 1 if published else None
        cur.execute(
            "UPDATE ledger.recon_run SET state='COMPLETE',result_sha256=%s,semantic_sha256=%s,"
            "published=%s,published_epoch=%s,finished_at=clock_timestamp() WHERE workspace_id=%s "
            "AND run_id=%s",
            (final_ref.sha256, semantic, published, published_epoch, workspace, run_id),
        )
        if published:
            cur.execute(
                "UPDATE ledger.recon_registry_guard SET evidence_epoch=%s WHERE workspace_id=%s",
                (published_epoch, workspace),
            )
            cur.execute(
                "INSERT INTO "
                "ledger.recon_current(workspace_id,provider_namespace,business_date,"
                "run_id,evidence_epoch) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,provider_namespace,business_date) "
                "DO UPDATE SET run_id=EXCLUDED.run_id,evidence_epoch=EXCLUDED.evidence_epoch",
                (
                    workspace,
                    manifest["provider_namespace"],
                    manifest["business_date"],
                    run_id,
                    published_epoch,
                ),
            )
        return Completion(run_id, final_ref.sha256, semantic, published, published_epoch)


def fail_run(conn, workspace_id, *, run_id, code):
    workspace, run_id = _uuid(workspace_id), _uuid(run_id)
    if type(code) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
        raise PublicationError("INVALID_FAILURE_CODE")
    with _transaction(conn) as cur:
        _registry(cur, workspace)
        run = _run(cur, workspace, run_id)
        if run["state"] == "FAILED" and run["failure_code"] == code:
            return "ALREADY_FAILED"
        if run["state"] != "STAGING":
            raise PublicationError("RUN_NOT_STAGING")
        cur.execute(
            "UPDATE ledger.recon_run SET "
            "state='FAILED',failure_code=%s,finished_at=clock_timestamp() "
            "WHERE workspace_id=%s AND run_id=%s",
            (code, workspace, run_id),
        )
        return "FAILED"


def pin_run(
    conn,
    workspace_id,
    store,
    *,
    reader_id,
    run_id=None,
    provider_namespace=None,
    business_date=None,
):
    """Pin exactly one COMPLETE run and verify every referenced object, without fallback."""
    from ledgerops.reconciliation import semantic_digest

    workspace, reader_id = _uuid(workspace_id), _uuid(reader_id)
    with _transaction(conn) as cur:
        evidence_epoch = _registry(cur, workspace)
        if run_id is None:
            row = cur.execute(
                "SELECT run_id FROM ledger.recon_current WHERE workspace_id=%s AND "
                "provider_namespace=%s "
                "AND business_date=%s",
                (workspace, _uuid(provider_namespace), business_date),
            ).fetchone()
            if row is None:
                raise PublicationError("NO_CURRENT_RUN")
            run_id = row["run_id"]
        run_id = _uuid(run_id)
        run = _run(cur, workspace, run_id)
        if run["state"] != "COMPLETE":
            raise PublicationError("RUN_NOT_COMPLETE")
        current = cur.execute(
            "SELECT run_id FROM ledger.recon_current WHERE workspace_id=%s "
            "AND provider_namespace=%s "
            "AND business_date=%s",
            (workspace, run["provider_namespace"], run["business_date"]),
        ).fetchone()
        is_current = current is not None and current["run_id"] == run_id
        previous = cur.execute(
            "SELECT DISTINCT run_id FROM ledger.evidence_pin WHERE workspace_id=%s AND "
            "owner_type='READER' "
            "AND owner_id=%s",
            (workspace, reader_id),
        ).fetchall()
        if any(row["run_id"] != run_id for row in previous):
            raise PublicationError("READER_PIN_IDENTITY_CONFLICT")
        references = cur.execute(
            "SELECT p.object_sha256,o.byte_length FROM ledger.evidence_pin p JOIN "
            "ledger.recon_object o "
            "ON o.workspace_id=p.workspace_id AND o.sha256=p.object_sha256 "
            "WHERE p.workspace_id=%s AND p.owner_type='RUN' AND p.run_id=%s ORDER BY "
            "p.object_sha256",
            (workspace, run_id),
        ).fetchall()
        for row in references:
            _pin(cur, workspace, row["object_sha256"], "READER", reader_id, run_id)
    # Pins are now durable even if this process dies while reading. Failure
    # retains them for explicit release; age alone is never deletion authority.
    artifact_raw = None
    for row in references:
        raw = store.read(workspace, row["object_sha256"], row["byte_length"])
        if row["object_sha256"] == run["result_sha256"]:
            artifact_raw = raw
    if artifact_raw is None:
        raise PublicationError("RUN_REFERENCE_MISSING")
    artifact = _strict_json(artifact_raw)
    if (
        artifact.get("run_id") != str(run_id)
        or artifact.get("input_manifest_sha256") != run["manifest_sha256"]
        or artifact.get("partition_plan_sha256") != run["plan_sha256"]
        or artifact.get("semantic_sha256") != run["semantic_sha256"]
        or semantic_digest(artifact.get("result")) != run["semantic_sha256"]
    ):
        raise PublicationError("CORRUPT_RUN_ARTIFACT")
    return PinnedRun(
        workspace,
        run_id,
        reader_id,
        run["result_sha256"],
        run["semantic_sha256"],
        _strict_json(run["manifest"]),
        _strict_json(run["partition_plan"]),
        artifact["result"],
        run["published_epoch"],
        is_current,
        evidence_epoch,
    )


def release_reader_pin(conn, workspace_id, *, reader_id):
    workspace, reader_id = _uuid(workspace_id), _uuid(reader_id)
    with _transaction(conn) as cur:
        _registry(cur, workspace)
        rows = cur.execute(
            "SELECT DISTINCT run_id FROM ledger.evidence_pin WHERE workspace_id=%s AND "
            "owner_type='READER' "
            "AND owner_id=%s ORDER BY run_id",
            (workspace, reader_id),
        ).fetchall()
        for row in rows:
            _run(cur, workspace, row["run_id"])
        return cur.execute(
            "DELETE FROM ledger.evidence_pin WHERE workspace_id=%s AND owner_type='READER' AND "
            "owner_id=%s",
            (workspace, reader_id),
        ).rowcount
