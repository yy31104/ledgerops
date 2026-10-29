"""Explicit compact matcher/target views backed by a complete pinned stream10.

Opaque request, transport and quarantine history stays in the original chunks and
independent report. This view never claims to be a complete legacy export.
"""

import hashlib
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ledgerops.reconciliation import PARSER_VERSION, RULE_VERSION, PreparedInputs
from ledgerops.reconciliation.inputs import collect_ledger
from ledgerops.reconciliation.planning import build_plan
from ledgerops.reconciliation.rules import Issues, inspect_sources
from ledgerops.reconciliation.statement import CorruptInput, canonical, decode, parse_statement
from ledgerops.stream_verification import stage_stream, verify_store

KIND = "FINANCIAL_STREAM10_COMPACT_V1"
MAX_COMPACT_BYTES = 16 * 1024 * 1024
MATCHER = ("accounts", "operations", "journals", "entries", "payouts", "payout_resolutions")
TARGET = (
    "accounts",
    "balances",
    "payouts",
    "reservations",
    "provider_observations",
    "payout_incidents",
    "related_incidents",
    "payout_resolutions",
)
OBSERVATION = ("workspace_id", "payout_id", "observation_id", "envelope_sha256", "kind", "state")


def reference(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def compact_from_store(store, report):
    if store.source.get("capture_context") is not None:
        raise CorruptInput("RECONCILIATION_REQUIRES_REPEATABLE_READ_SOURCE")
    if report.get("verification_complete") is not True:
        raise CorruptInput("INCOMPLETE_STREAM_VERIFICATION")
    used, selected = 0, {}
    for table in dict.fromkeys((*MATCHER, *TARGET)):
        rows = []
        for row in store.rows(table):
            if table == "provider_observations":
                row = {field: row[field] for field in OBSERVATION}
            used += len(canonical(row))
            if used > MAX_COMPACT_BYTES:
                raise CorruptInput("COMPACT_MATCHER_CAPACITY")
            rows.append(row)
        selected[table] = rows
    value = {
        "kind": KIND,
        "source_manifest": reference(store.manifest_raw),
        "source_content_sha256": store.content_sha256,
        "source": store.source,
        "row_counts": store.manifest["row_counts"],
        "chunks": store.manifest["chunks"],
        "verification": report,
        "matcher": {name: selected[name] for name in MATCHER},
        "targets": {name: selected[name] for name in TARGET},
    }
    raw = canonical(value)
    if len(raw) > MAX_COMPACT_BYTES:
        raise CorruptInput("COMPACT_MATCHER_CAPACITY")
    return value, raw


def build_compact(path, *, scratch_root=None):
    rejected = None
    with stage_stream(path, scratch_root=scratch_root) as store:
        try:
            result = compact_from_store(store, verify_store(store))
        except CorruptInput as exc:
            rejected = exc
    if rejected is not None:
        raise rejected
    return result


def internal_report(compact):
    report = {
        **compact["verification"],
        "external_evidence": {**compact["verification"]["external_evidence"]},
    }
    resolutions = {row["payout_id"] for row in compact["matcher"]["payout_resolutions"]}
    report["external_evidence"]["unresolved_payout_ids"] = sorted(
        row["operation_id"]
        for row in compact["matcher"]["payouts"]
        if row["operation_id"] not in resolutions
    )
    return report


def target_snapshot(compact):
    return {
        "format_version": 9,
        "stream_source_kind": KIND,
        **compact["source"],
        **compact["targets"],
    }


def load_pinned_compact(compact_raw, manifest_raw, read_chunk, *, scratch_root=None):
    """Rehydrate exact pinned bytes, independently verify, and compare the derived view."""
    if type(compact_raw) is not bytes or len(compact_raw) > MAX_COMPACT_BYTES:
        raise CorruptInput("COMPACT_MATCHER_CAPACITY")
    compact = decode(compact_raw)
    if type(compact) is not dict or compact.get("kind") != KIND:
        raise CorruptInput("INVALID_STREAM_COMPACT_KIND")
    if compact.get("source_manifest") != reference(manifest_raw):
        raise CorruptInput("COMPACT_SOURCE_BINDING")
    from ledgerops.stream_verification import _manifest

    manifest = _manifest(manifest_raw)
    total = len(manifest_raw)
    names = set()
    for chunk in manifest["chunks"]:
        if (
            type(chunk) is not dict
            or type(chunk.get("bytes")) is not int
            or not 1 <= chunk["bytes"] <= 4 * 1024 * 1024
            or type(chunk.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", chunk["sha256"]) is None
            or type(chunk.get("file")) is not str
            or chunk["file"] in names
        ):
            raise CorruptInput("INVALID_STREAM_CHUNK_REFERENCE")
        names.add(chunk["file"])
        total += chunk["bytes"]
    if total != manifest["total_bytes"]:
        raise CorruptInput("STREAM_CHUNK_TOTAL_MISMATCH")
    with tempfile.TemporaryDirectory(
        prefix="ledgerops-pinned-stream-", dir=scratch_root
    ) as temporary:
        root = Path(temporary) / manifest["export_id"]
        root.mkdir(mode=0o700)
        (root / "manifest.json").write_bytes(manifest_raw)
        for chunk in manifest["chunks"]:
            name = chunk["file"]
            if type(name) is not str or re.fullmatch(r"[a-z_]+\.[0-9]{6}\.jsonl", name) is None:
                raise CorruptInput("INVALID_STREAM_CHUNK_NAME")
            raw = read_chunk(chunk)
            if reference(raw) != {"sha256": chunk["sha256"], "bytes": chunk["bytes"]}:
                raise CorruptInput("PINNED_STREAM_CHUNK_MISMATCH")
            (root / name).write_bytes(raw)
        with stage_stream(root, scratch_root=scratch_root) as store:
            derived, raw = compact_from_store(store, verify_store(store))
    if raw != compact_raw:
        raise CorruptInput("COMPACT_SOURCE_REPORT_MISMATCH")
    return derived


@dataclass(frozen=True)
class StreamPrepared:
    prepared: PreparedInputs
    compact_bytes: bytes
    manifest_bytes: bytes
    statement_bytes: bytes
    directory: Path

    def sources(self):
        yield "ledger_manifest", self.manifest_bytes
        yield "ledger_data", self.compact_bytes
        yield "statement", self.statement_bytes
        manifest = decode(self.manifest_bytes)
        for ordinal, chunk in enumerate(manifest["chunks"]):
            yield f"stream_chunk_{ordinal:06d}", (self.directory / chunk["file"]).read_bytes()


def _coverage(plan, refs, ledger_hash):
    marker = f"{ledger_hash}:ledger_metadata:0"
    global_unit = next(unit for unit in plan["units"] if marker in unit["coverage"])
    additions = [f"{ref['sha256']}:stream_chunk:{ordinal}" for ordinal, ref in enumerate(refs)]
    global_unit["coverage"] = sorted(global_unit["coverage"] + additions)
    for partition in plan["partitions"]:
        if global_unit["unit_id"] in partition["unit_ids"]:
            partition["coverage"] = sorted(partition["coverage"] + additions)
    plan["expected_coverage"] = sorted(plan["expected_coverage"] + additions)


def prepare_stream_inputs(
    path, statement_bytes, *, trust, code_revision, dirty=False, partitions=16, scratch_root=None
):
    if type(code_revision) is not str or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None:
        raise CorruptInput("MATCHER_CODE_REVISION_REQUIRED")
    if type(dirty) is not bool:
        raise CorruptInput("INVALID_DIRTY_FLAG")
    compact, raw = build_compact(path, scratch_root=scratch_root)
    manifest_raw = (Path(path) / "manifest.json").read_bytes()
    if reference(manifest_raw) != compact["source_manifest"]:
        raise CorruptInput("STREAM_CHANGED_DURING_PREPARATION")
    manifest = decode(manifest_raw)
    snapshot = {"format_version": 9, **compact["source"], **compact["matcher"]}
    statement = parse_statement(statement_bytes, trust=trust)
    if snapshot["workspace"]["id"] != statement["workspace_id"]:
        raise CorruptInput("INPUT_WORKSPACE_MISMATCH")
    issues = Issues()
    facts = collect_ledger(snapshot, issues)
    effects = inspect_sources(statement, issues)
    refs = {
        "ledger_manifest": reference(manifest_raw),
        "ledger_data": reference(raw),
        "statement": reference(statement_bytes),
    }
    source_chunks = []
    for ordinal, chunk in enumerate(manifest["chunks"]):
        name = f"stream_chunk_{ordinal:06d}"
        refs[name] = {"sha256": chunk["sha256"], "bytes": chunk["bytes"]}
        source_chunks.append({"name": name, **chunk})
    bindings = {
        "manifest_version": 2,
        "source_kind": KIND,
        "source_chunks": source_chunks,
        "inputs": refs,
        "workspace_id": facts.workspace,
        "provider_namespace": statement["namespace"],
        "business_date": statement["business_date"],
        "timezone": statement["timezone"],
        "start_inclusive": statement["start_inclusive"],
        "end_exclusive": statement["end_exclusive"],
        "statement_version": statement["version"],
        "statement_key_id": decode(statement_bytes)["key_id"],
        "effect_watermark": statement["effect_watermark"],
        "assignment_watermark": statement["assignment_watermark"],
        "export_id": manifest["export_id"],
        "ledger_source_code_revision": manifest["code_revision"],
        "ledger_source_dirty": manifest["dirty"],
        "rule_version": RULE_VERSION,
        "parser_version": PARSER_VERSION,
        "code_revision": code_revision,
        "dirty": dirty,
    }
    plan = build_plan(
        snapshot,
        statement,
        ledger_sha256=refs["ledger_data"]["sha256"],
        statement_sha256=refs["statement"]["sha256"],
        manifest_sha256=refs["ledger_manifest"]["sha256"],
        partitions=partitions,
    )
    _coverage(plan, manifest["chunks"], refs["ledger_data"]["sha256"])
    evidence_keys = defaultdict(set)
    for effect in statement["effects"]:
        key = effect["provider_key"]
        for identity in (
            effect["request"]["operation_id"],
            str(effect["execution_sequence"]),
            "statement",
        ):
            evidence_keys[identity].add(key)
        if effect["assignment"] is not None:
            for field in ("batch_id", "row_id", "sequence"):
                identity = effect["assignment"][field]
                if identity is not None:
                    evidence_keys[str(identity)].add(key)
    for row in statement["rows"]:
        evidence_keys[row["row_id"]].update(row["provider_keys"] or ())
    prepared = PreparedInputs(
        bindings,
        plan,
        facts,
        statement,
        effects,
        {key: tuple(sorted(values)) for key, values in evidence_keys.items()},
        tuple(issues.sorted()),
        internal_report(compact),
    )
    return StreamPrepared(prepared, raw, manifest_raw, statement_bytes, Path(path))


def run_stream_reconciliation(
    conn,
    store,
    path,
    statement_bytes,
    *,
    trust,
    code_revision,
    dirty=False,
    workers=1,
    partitions=16,
    timeout_seconds=300,
    run_id=None,
    scratch_root=None,
):
    """Register/pin every source chunk, then use the established bounded matcher."""
    import time
    from uuid import UUID, uuid4

    from ledgerops.reconciliation.publication import (
        accept_partition,
        begin_run,
        complete_run,
        fail_run,
        partition_envelope,
        register_object,
    )
    from ledgerops.reconciliation.runner import RunError, RunResult, _compute_partitions, _limits

    _limits(workers, timeout_seconds)
    if workers != 1:
        raise RunError("STREAM_SERIAL_WORKER_REQUIRED")
    try:
        identifier = uuid4() if run_id is None else UUID(str(run_id))
        if run_id is not None and str(identifier) != str(run_id):
            raise ValueError("noncanonical run ID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RunError("INVALID_RUN_ID") from exc
    started = time.perf_counter()
    begun = False
    workspace = None
    try:
        source = prepare_stream_inputs(
            path,
            statement_bytes,
            trust=trust,
            code_revision=code_revision,
            dirty=dirty,
            partitions=partitions,
            scratch_root=scratch_root,
        )
        prepared = source.prepared
        workspace = prepared.input_manifest["workspace_id"]
        pins = {}
        for name, raw in source.sources():
            if reference(raw) != prepared.input_manifest["inputs"][name]:
                raise CorruptInput("STREAM_CHANGED_DURING_REGISTRATION")
            pins[name] = register_object(conn, workspace, store, raw, publisher_id=identifier)
        begun = True
        begin_run(conn, store, prepared, run_id=identifier, input_pins=pins)

        def publish(part):
            raw = canonical(partition_envelope(prepared, identifier, part))
            ref = register_object(conn, workspace, store, raw, publisher_id=identifier)
            accept_partition(
                conn,
                workspace,
                store,
                run_id=identifier,
                partition_id=part["partition_id"],
                result_ref=ref,
            )

        _, metrics = _compute_partitions(
            prepared, workers=workers, timeout_seconds=timeout_seconds, on_result=publish
        )
        completion = complete_run(conn, workspace, store, prepared, run_id=identifier)
        metrics.update(
            elapsed_seconds=time.perf_counter() - started,
            source_kind=KIND,
            source_objects=len(pins),
            compact_bytes=len(source.compact_bytes),
        )
        return RunResult(completion, metrics)
    except BaseException as exc:
        failed = False
        if begun:
            try:
                failed = fail_run(conn, workspace, run_id=identifier, code="STREAM_RUN_FAILED") in {
                    "FAILED",
                    "ALREADY_FAILED",
                }
            except Exception:
                pass
        raise RunError(
            "STREAM_RUN_FAILED",
            run_id=identifier,
            failure_recorded=failed,
            outcome_unknown=begun and not failed,
        ) from exc


def match_prepared(prepared):
    from ledgerops.reconciliation import match_partition, merge_partition_results

    return merge_partition_results(
        prepared,
        [
            match_partition(prepared, part["partition_id"])
            for part in prepared.partition_plan["partitions"]
        ],
    )
