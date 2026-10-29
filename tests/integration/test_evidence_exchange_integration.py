"""Actual source snapshot -> selected exchange -> isolated independent consumer."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ledgerops.exchange import publish_exchange
from ledgerops.exchange_consumer import load_exchange
from ledgerops.export import export_workspace, load_export_bundle
from ledgerops.reconciliation import publication
from tests.integration.test_operator_actions import approve, execute, operator_story, propose
from tests.integration.test_reconciliation_publication import REVISION


def test_real_source_snapshot_retains_selected_effect_unknown_times_and_old_bytes(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path)
    proposal = propose(story)
    approve(story, proposal)
    execute(story, proposal)
    with seeded_db.connect("reader") as conn:
        original = export_workspace(
            conn,
            str(story["workspace"]),
            tmp_path / "after-execution",
            code_revision=REVISION,
            dirty=True,
        )
    bundle = load_export_bundle(original)
    directory = publish_exchange(
        bundle, tmp_path / "exchange", payout_ids=[str(story["payout_id"])]
    )
    manifest, records, summary = load_exchange(directory)
    assert manifest["source"]["code_revision"] == REVISION and manifest["source"]["dirty"] is True
    assert summary["payout_count"] == summary["journal_count"] == "1"
    assert summary["journal_totals"][0]["debit_minor"] == "62"
    assert len(records["exceptions"]) == len(records["dispositions"]) == 1
    assert records["observations"][0]["external_event_at"] is None
    assert records["resolutions"][0]["financial_commit_at"] is None
    assert (
        records["resolutions"][0]["resolved_at"]
        == bundle.snapshot["payout_resolutions"][0]["resolved_at"]
    )
    original_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }
    # Later ledger state does not change already published selected evidence.
    with seeded_db.connect("owner") as conn:
        conn.execute("UPDATE ledger.account_balance SET version=version+1")
    assert load_exchange(directory)[2] == summary
    assert original_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }
    importer = tmp_path / "standalone_consumer.py"
    import ledgerops.exchange_consumer as consumer

    shutil.copyfile(Path(consumer.__file__), importer)
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
    assert json.loads(result.stdout) == summary


def test_real_pinned_reconciliation_reference_preserves_exact_source_and_watermarks(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path)
    reader = uuid4()
    with seeded_db.connect("reader") as conn:
        try:
            pinned = publication.pin_run(
                conn,
                story["workspace"],
                story["store"],
                run_id=story["evidence_run_id"],
                reader_id=reader,
            )
            raw = (
                json.dumps(asdict(pinned), default=str, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        finally:
            publication.release_reader_pin(conn, story["workspace"], reader_id=reader)
    from ledgerops.export import LoadedExport

    source = LoadedExport(story["raws"]["ledger_manifest"], story["raws"]["ledger_data"])
    directory = publish_exchange(
        source, tmp_path / "exchange", payout_ids=[str(story["payout_id"])], pinned_reads=[raw]
    )
    manifest, records, summary = load_exchange(directory)
    reference = records["reconciliation_references"][0]
    assert reference["run_id"] == str(story["evidence_run_id"])
    assert reference["ledger_data_sha256"] == manifest["source"]["data_sha256"]
    assert reference["reader_pin_id"] == str(reader)
    assert reference["effect_watermark"] == str(pinned.manifest["effect_watermark"])
    assert reference["assignment_watermark"] == str(pinned.manifest["assignment_watermark"])
    assert reference["business_date"] == pinned.manifest["business_date"]
    assert reference["reference_status"] == "HISTORICAL_PIN_REFERENCE_NOT_LIVE_AUTHORITY"
    assert summary["journal_count"] == "0"
