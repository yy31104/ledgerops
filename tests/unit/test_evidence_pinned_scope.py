"""Hash-recomputed contradictory pinned R4 scope/version must fail closed."""

import copy
import hashlib
from uuid import uuid4

import pytest
from test_payout_external_verification import v6
from test_payout_verification import PAYOUT, REVISION, for_export
from test_verification import snapshot as snapshot

from ledgerops import exchange
from ledgerops.exchange_consumer import ExchangeError, canonical, decode
from ledgerops.export import load_export_bundle, publish_snapshot
from tests.unit.stream_fixture_helpers import source, write


def compact(value):
    return canonical(value)[:-1]


def sha(value):
    return hashlib.sha256(compact(value)).hexdigest()


def pin(manifest, workspace):
    result = {
        "report_version": 1,
        "status": "COMPLETE",
        "usable": True,
        "inputs": manifest,
        **{
            key: manifest[key]
            for key in (
                "workspace_id",
                "provider_namespace",
                "business_date",
                "timezone",
                "statement_version",
                "rule_version",
                "parser_version",
            )
        },
        "window": {key: manifest[key] for key in ("start_inclusive", "end_exclusive")},
    }
    if manifest.get("source_kind") == "FINANCIAL_STREAM10_COMPACT_V1":
        result["internal_verification"] = {
            "source_manifest_sha256": manifest["inputs"]["ledger_manifest"]["sha256"],
            "source_content_sha256": manifest.pop("_stream_content_sha256"),
        }
    run = str(uuid4())
    plan = {}
    artifact = {
        "run_id": run,
        "input_manifest_sha256": sha(manifest),
        "partition_plan_sha256": sha(plan),
        "semantic_sha256": sha(result),
        "result": result,
    }
    return {
        "workspace_id": workspace,
        "run_id": run,
        "reader_id": str(uuid4()),
        "result_sha256": sha(artifact),
        "semantic_sha256": sha(result),
        "manifest": manifest,
        "partition_plan": plan,
        "result": result,
        "published_epoch": 1,
        "is_current_at_pin": True,
        "evidence_epoch_at_pin": 1,
    }


def rehash(value):
    value["semantic_sha256"] = sha(value["result"])
    artifact = {
        "run_id": value["run_id"],
        "input_manifest_sha256": sha(value["manifest"]),
        "partition_plan_sha256": sha(value["partition_plan"]),
        "semantic_sha256": value["semantic_sha256"],
        "result": value["result"],
    }
    value["result_sha256"] = sha(artifact)
    return canonical(value)


@pytest.fixture(params=["legacy", "stream"])
def pinned_source(request, snapshot, tmp_path):
    data = v6(for_export(snapshot))
    if request.param == "legacy":
        path = publish_snapshot(data, tmp_path / "source", code_revision=REVISION, dirty=False)
        bundle = load_export_bundle(path)
        manifest_raw, _ = exchange.build_exchange(bundle, payout_ids=[PAYOUT])

        def build(raw):
            return exchange.build_exchange(bundle, payout_ids=[PAYOUT], pinned_reads=[raw])
    else:
        path = write(source(data), tmp_path / "source")
        manifest_raw, _ = exchange.build_stream_exchange(path, payout_ids=[PAYOUT])

        def build(raw):
            return exchange.build_stream_exchange(path, payout_ids=[PAYOUT], pinned_reads=[raw])

    source_metadata = decode(manifest_raw)["source"]
    manifest = {
        "manifest_version": 1 if request.param == "legacy" else 2,
        "workspace_id": source_metadata["workspace_id"],
        "provider_namespace": data["payouts"][0]["provider_namespace"],
        "business_date": "2026-10-29",
        "timezone": "Europe/Warsaw",
        "start_inclusive": "2026-10-28T23:00:00Z",
        "end_exclusive": "2026-10-29T23:00:00Z",
        "effect_watermark": 1,
        "assignment_watermark": 1,
        "statement_version": 1,
        "export_id": source_metadata["export_id"],
        "ledger_source_code_revision": source_metadata["code_revision"],
        "ledger_source_dirty": source_metadata["dirty"],
        "rule_version": "ledgerops.reconciliation.v1",
        "parser_version": 1,
        "inputs": {
            "ledger_data": {"sha256": source_metadata["data_sha256"], "bytes": 1},
            "ledger_manifest": {"sha256": source_metadata["manifest_sha256"], "bytes": 1},
            "statement": {"sha256": "a" * 64, "bytes": 1},
        },
    }
    if request.param == "stream":
        manifest.update(
            source_kind="FINANCIAL_STREAM10_COMPACT_V1",
            _stream_content_sha256=source_metadata["data_sha256"],
        )
        manifest["inputs"]["ledger_data"]["sha256"] = "b" * 64
    return build, pin(manifest, source_metadata["workspace_id"])


def test_matching_supported_pin_metadata_remains_accepted(pinned_source):
    build, pinned = pinned_source
    _, records_raw = build(rehash(pinned))
    assert len(decode(records_raw)["reconciliation_references"]) == 1


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("workspace", "RECONCILIATION_SCOPE_MISMATCH"),
        ("version", "UNSUPPORTED_RECONCILIATION_SOURCE_VERSION"),
        ("bool_version", "UNSUPPORTED_RECONCILIATION_SOURCE_VERSION"),
        ("source_kind", "UNSUPPORTED_RECONCILIATION_SOURCE_VERSION"),
        ("result_workspace", "RECONCILIATION_SCOPE_MISMATCH"),
        ("result_date", "RECONCILIATION_SCOPE_MISMATCH"),
        ("result_namespace", "RECONCILIATION_SCOPE_MISMATCH"),
        ("result_window", "RECONCILIATION_SCOPE_MISMATCH"),
        ("result_statement_version", "RECONCILIATION_SCOPE_MISMATCH"),
        ("export_id", "RECONCILIATION_SOURCE_MISMATCH"),
        ("manifest_sha", "RECONCILIATION_SOURCE_MISMATCH"),
        ("revision", "RECONCILIATION_SOURCE_MISMATCH"),
        ("dirty", "RECONCILIATION_SOURCE_MISMATCH"),
    ],
)
def test_recomputed_pin_hashes_cannot_hide_different_scope_version_or_source(
    pinned_source, mutation, code
):
    build, original = pinned_source
    pinned = copy.deepcopy(original)
    manifest, result = pinned["manifest"], pinned["result"]
    if mutation == "workspace":
        manifest["workspace_id"] = str(uuid4())
        result["workspace_id"] = manifest["workspace_id"]
    elif mutation == "version":
        manifest["manifest_version"] = 900
    elif mutation == "bool_version":
        manifest["manifest_version"] = True
    elif mutation == "source_kind":
        manifest["source_kind"] = "FUTURE_UNKNOWN_SEMANTICS"
    elif mutation == "result_workspace":
        result["workspace_id"] = str(uuid4())
    elif mutation == "result_date":
        result["business_date"] = "2026-10-30"
    elif mutation == "result_namespace":
        result["provider_namespace"] = str(uuid4())
    elif mutation == "result_window":
        result["window"]["end_exclusive"] = "2026-10-30T23:00:00Z"
    elif mutation == "result_statement_version":
        result["statement_version"] = 2
    elif mutation == "export_id":
        manifest["export_id"] = str(uuid4())
    elif mutation == "manifest_sha":
        manifest["inputs"]["ledger_manifest"]["sha256"] = "c" * 64
    elif mutation == "revision":
        manifest["ledger_source_code_revision"] = "c" * 40
    elif mutation == "dirty":
        manifest["ledger_source_dirty"] = not manifest["ledger_source_dirty"]
    with pytest.raises(ExchangeError, match=code):
        build(rehash(pinned))
