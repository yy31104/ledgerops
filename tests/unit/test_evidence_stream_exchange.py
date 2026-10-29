"""Explicit exchange minor1 stream provenance; historical v1 bytes stay frozen."""

import hashlib
import json
from pathlib import Path

import pytest
from test_verification import snapshot as snapshot

from ledgerops.exchange import publish_stream_exchange
from ledgerops.exchange_consumer import ExchangeError, canonical, load_exchange
from tests.unit.stream_fixture_helpers import source, write

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures/golden"


def test_new_stream_source_uses_explicit_minor1_with_full_bound_verdict(snapshot, tmp_path):
    data = source(snapshot)
    header = next(row for row in data["journals"] if row["kind"] == "PURCHASE")
    original = write(data, tmp_path / "source")
    selected = publish_stream_exchange(
        original, tmp_path / "selected", journal_ids=[header["journal_id"]]
    )
    manifest, records, summary = load_exchange(selected)
    assert manifest["schema_major"] == 1 and manifest["schema_minor"] == 1
    assert manifest["source"]["source_kind"] == "FINANCIAL_STREAM10"
    assert (
        manifest["source"]["financial_format"] == "10" and manifest["source"]["row_format"] == "9"
    )
    assert manifest["source"]["full_verification"]["verification_complete"]
    assert manifest["source"]["full_verification"]["healthy"]
    assert summary["source_healthy"] and summary["source_verification_complete"]
    assert summary["journal_totals"][0]["debit_minor"] == "80"
    assert len(records["accounts"]) == 2 and len(records["journals"]) == 1
    assert (
        manifest["source"]["data_sha256"]
        == hashlib.sha256(
            b"".join(
                (original / chunk["file"]).read_bytes()
                for chunk in json.loads((original / "manifest.json").read_bytes())["chunks"]
            )
        ).hexdigest()
    )
    # Extra provenance cannot be silently interpreted under historical minor0.
    manifest["schema_minor"] = 0
    path = selected / "manifest.json"
    path.chmod(0o600)
    path.write_bytes(canonical(manifest))
    with pytest.raises(ExchangeError, match="UNKNOWN_OR_MISSING_FIELDS"):
        load_exchange(selected)


def test_global_source_failure_is_preserved_in_selected_evidence(snapshot, tmp_path):
    data = source(snapshot)
    data["balances"][0]["debits"] = "99"
    header = next(row for row in data["journals"] if row["kind"] == "PURCHASE")
    original = write(data, tmp_path / "source")
    selected = publish_stream_exchange(
        original, tmp_path / "selected", journal_ids=[header["journal_id"]]
    )
    manifest, records, summary = load_exchange(selected)
    assert manifest["source"]["full_verification"]["verification_complete"]
    assert not manifest["source"]["full_verification"]["projection_ok"]
    assert not summary["source_healthy"]
    assert summary["source_verification_complete"]
    assert len(records["journals"]) == 1


def test_frozen_v1_hashes_and_independent80_golden_survive_new_stream_feature():
    for line in (GOLDEN / "exchange_v1.sha256").read_text().splitlines():
        expected, name = line.split("  ")
        assert hashlib.sha256((GOLDEN / name).read_bytes()).hexdigest() == expected
    manifest, records, summary = load_exchange(GOLDEN / "exchange_v1")
    expected = json.loads((GOLDEN / "exchange_v1.expected.json").read_bytes())
    assert summary == expected
    assert manifest["schema_minor"] == 0
    assert summary["journal_totals"] == [
        {
            "journal_id": "50000000-0000-4000-8000-000000000002",
            "debit_minor": "80",
            "credit_minor": "80",
        }
    ]
    assert records["payouts"] == []
