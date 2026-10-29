"""Frozen selected evidence and independently imported literal histories."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_payout_external_verification import finish, observe, v6
from test_payout_verification import PAYOUT, REVISION, for_export
from test_verification import snapshot as snapshot

from ledgerops import exchange
from ledgerops.exchange_consumer import ExchangeError, canonical, load_exchange
from ledgerops.export import load_export_bundle, publish_snapshot

STAGE = Path(__file__).resolve().parents[2]


def bundle(data, tmp_path):
    path = publish_snapshot(data, tmp_path / "source", code_revision=REVISION, dirty=False)
    return load_export_bundle(path)


@pytest.fixture
def selected_bundle(snapshot, tmp_path):
    for_export(snapshot)
    v6(snapshot)
    finish(snapshot, observe(snapshot))
    return bundle(snapshot, tmp_path)


def rewrite(directory, manifest=None, records=None):
    manifest = manifest or json.loads((directory / "manifest.json").read_bytes())
    records = records or json.loads((directory / "records.json").read_bytes())
    raw = canonical(records)
    manifest["records"]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest["records"]["bytes"] = str(len(raw))
    manifest["records"]["row_counts"] = {name: str(len(rows)) for name, rows in records.items()}
    for name, value in (("manifest.json", canonical(manifest)), ("records.json", raw)):
        path = directory / name
        path.chmod(0o600)
        path.write_bytes(value)


def test_selected_payout_only_closes_its_own_journal_accounts_and_opaque_owners(
    selected_bundle, tmp_path
):
    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    manifest, records, summary = load_exchange(directory)
    assert manifest["units"] == {"currency": "PLN", "money_unit": "PLN_minor", "currency_scale": 2}
    assert manifest["selection"]["payout_ids"] == [PAYOUT]
    assert len(records["accounts"]) == 2
    assert len(records["journals"]) == 1 and len(records["entries"]) == 2
    source_owners = {row["id"]: row["owner_id"] for row in selected_bundle.snapshot["accounts"]}
    assert all(source_owners[row["account_id"]] == row["owner_id"] for row in records["accounts"])
    assert records["payouts"][0]["gross_minor"] == "62"
    assert records["payouts"][0]["source_sequence"] == "1"
    assert records["observations"][0]["external_event_at"] is None
    assert records["resolutions"][0]["financial_commit_at"] is None
    assert manifest["source"]["snapshot_at"] is None
    assert (
        records["observations"][0]["received_at"]
        == selected_bundle.snapshot["provider_observations"][0]["received_at"]
    )
    assert (
        summary["journal_totals"][0]["debit_minor"]
        == summary["journal_totals"][0]["credit_minor"]
        == "62"
    )
    raw = (directory / "records.json").read_text()
    for forbidden in (
        "signature",
        "signed_payload",
        "envelope_hex",
        "provider_request",
        "maker_actor_id",
        "client_id",
        "token",
        "auth_generation",
    ):
        assert forbidden not in raw


def test_identical_selection_and_source_are_deterministic_and_immutable(selected_bundle, tmp_path):
    first = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT, PAYOUT]
    )
    before = {path.name: path.read_bytes() for path in first.iterdir()}
    second = exchange.publish_exchange(selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT])
    assert first == second
    assert {path.name: path.read_bytes() for path in first.iterdir()} == before
    assert not (first / "records.json").stat().st_mode & 0o222
    assert (
        selected_bundle.data_bytes
        == (tmp_path / "source" / selected_bundle.manifest["export_id"] / "data.json").read_bytes()
    )


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda m: m.update(schema_major=2), "UNSUPPORTED_SCHEMA_VERSION"),
        (lambda m: m.update(schema_minor=2), "UNSUPPORTED_SCHEMA_VERSION"),
        (lambda m: m["units"].update(money_unit="PLN_major"), "UNSUPPORTED_UNITS"),
        (lambda m: m["units"].update(currency_scale=True), "UNSUPPORTED_UNITS"),
        (lambda m: m.update(semantics="guess-finality:v1"), "UNSUPPORTED_SEMANTICS"),
        (
            lambda m: m.update(control_callback="http://localhost/write"),
            "UNKNOWN_OR_MISSING_FIELDS",
        ),
        (
            lambda m: m["source"].update(snapshot_at="2026-10-29T10:00:00Z"),
            "UNSUPPORTED_TEMPORAL_SEMANTICS",
        ),
    ],
)
def test_consumer_rejects_unknown_manifest_contract_even_with_valid_records_hash(
    selected_bundle, tmp_path, change, code
):
    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    change(manifest)
    rewrite(directory, manifest=manifest)
    with pytest.raises(ExchangeError, match=code):
        load_exchange(directory)


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda r: r["payouts"][0].update(version=9007199254740993),
            "LOSSLESS_INTEGER_STRING_REQUIRED",
        ),
        (lambda r: r["payouts"][0].update(beneficiary_id=None), "UNSUPPORTED_NULL_SEMANTICS"),
        (
            lambda r: r["observations"][0].update(external_event_at="2026-10-29T10:00:00Z"),
            "UNSUPPORTED_TEMPORAL_SEMANTICS",
        ),
        (
            lambda r: r["observations"][0].update(state="PROBABLY_PAID"),
            "UNSUPPORTED_OBSERVATION_SEMANTICS",
        ),
        (
            lambda r: r["resolutions"][0].update(
                financial_commit_time_state="INFERRED_FROM_RESOLVED_AT"
            ),
            "UNSUPPORTED_TEMPORAL_SEMANTICS",
        ),
        (lambda r: r["accounts"][0].update(token="secret"), "UNKNOWN_OR_MISSING_FIELDS"),
        (
            lambda r: r["entries"].append(copy.deepcopy(r["entries"][0])),
            "UNSORTED_OR_DUPLICATE_IDENTITY",
        ),
    ],
)
def test_consumer_rejects_record_semantic_corruption_after_hash_recompute(
    selected_bundle, tmp_path, change, code
):
    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    records = json.loads((directory / "records.json").read_bytes())
    change(records)
    rewrite(directory, records=records)
    with pytest.raises(ExchangeError, match=code):
        load_exchange(directory)


def test_lossless_large_sequence_version_and_owner_identity(selected_bundle, tmp_path):
    data = selected_bundle.snapshot
    data["payouts"][0].update(source_sequence=9007199254740993, version=2**63 - 1)
    data["provider_observations"][0]["source_sequence"] = 9007199254740993
    source = bundle(data, tmp_path / "large")
    directory = exchange.publish_exchange(source, tmp_path / "exchange", payout_ids=[PAYOUT])
    records = load_exchange(directory)[1]
    assert records["payouts"][0]["source_sequence"] == "9007199254740993"
    assert records["payouts"][0]["version"] == "9223372036854775807"


def test_pre_v6_payout_does_not_invent_a_source_sequence(snapshot, tmp_path):
    from test_payout_verification import add_payout

    for_export(snapshot)
    add_payout(snapshot)
    directory = exchange.publish_exchange(
        bundle(snapshot, tmp_path), tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    record = load_exchange(directory)[1]["payouts"][0]
    assert record["source_sequence"] is None
    assert record["source_sequence_state"] == "UNKNOWN_SOURCE_FORMAT_PRE_V6"


def test_duplicate_fields_and_corrupt_file_hash_fail_closed(selected_bundle, tmp_path):
    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    manifest_path = directory / "manifest.json"
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(
        manifest_path.read_bytes().replace(
            b'"schema_major":1', b'"schema_major":1,"schema_major":1'
        )
    )
    with pytest.raises(ExchangeError, match="DUPLICATE_JSON_FIELD"):
        load_exchange(directory)
    other = exchange.publish_exchange(selected_bundle, tmp_path / "other", payout_ids=[PAYOUT])
    record_path = other / "records.json"
    record_path.chmod(0o600)
    record_path.write_bytes(record_path.read_bytes().replace(b'"62"', b'"63"', 1))
    with pytest.raises(ExchangeError, match="RECORDS_HASH_OR_SIZE_MISMATCH"):
        load_exchange(other)


def test_standalone_importer_runs_isolated_without_ledgerops_or_network(selected_bundle, tmp_path):
    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    importer = tmp_path / "consumer.py"
    shutil.copyfile(Path(exchange.__file__).with_name("exchange_consumer.py"), importer)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LEDGEROPS_") and key != "PYTHONPATH"
    }
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(importer), str(directory)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["payout_count"] == "1" and summary["journal_count"] == "1"
    assert summary["provider_authenticity"] == "NOT_VERIFIED_NO_TRUST_KEYS"
    assert summary["validation_scope"] == "SCHEMA_HASH_REFERENTIAL_SELECTION_ONLY"


def test_source_v1_frozen_journal_and_exchange_golden_import_offline():
    directory = STAGE / "fixtures/golden/exchange_v1"
    manifest, records, summary = load_exchange(directory)
    expected = json.loads((STAGE / "fixtures/golden/exchange_v1.expected.json").read_bytes())
    assert summary == expected
    assert manifest["source"]["financial_format"] == "1"
    assert records["payouts"] == [] and records["observations"] == []


def test_valid_content_cannot_be_relabelled_with_another_dataset_identity(
    selected_bundle, tmp_path
):
    from uuid import uuid4

    directory = exchange.publish_exchange(
        selected_bundle, tmp_path / "exchange", payout_ids=[PAYOUT]
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["dataset_id"] = str(uuid4())
    rewrite(directory, manifest)
    with pytest.raises(ExchangeError, match="DATASET_IDENTITY_MISMATCH"):
        load_exchange(directory)
