"""Selected stream exchange and pinned reconciliation provenance through actual components."""

import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from uuid import uuid4

from ledgerops.exchange import publish_stream_exchange
from ledgerops.exchange_consumer import load_exchange
from ledgerops.reconciliation.publication import pin_run, release_reader_pin
from tests.integration.test_operator_actions import operator_story
from tests.integration.test_stream_reconciliation import publish, source


def test_large_complete_source_exchange_and_current_r4_reference_import_without_ledgerops(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path / "story")
    path = source(story, tmp_path / "source")
    updated, result = publish(story, path)
    reader = uuid4()
    with seeded_db.connect("reader") as conn:
        try:
            pinned = pin_run(
                conn,
                str(story["workspace"]),
                story["store"],
                reader_id=reader,
                run_id=result.completion.run_id,
            )
            raw = (
                json.dumps(asdict(pinned), default=str, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        finally:
            release_reader_pin(conn, str(story["workspace"]), reader_id=reader)
    directory = publish_stream_exchange(
        path, tmp_path / "selected", payout_ids=[str(story["payout_id"])], pinned_reads=[raw]
    )
    manifest, records, summary = load_exchange(directory)
    assert manifest["schema_minor"] == 1 and summary["source_verification_complete"]
    assert summary["source_healthy"]
    assert len(records["payouts"]) == len(records["holds"]) == 1
    reference = records["reconciliation_references"][0]
    assert reference["run_id"] == str(result.completion.run_id)
    assert reference["full_stream_manifest_sha256"] == manifest["source"]["manifest_sha256"]
    assert reference["matcher_data_sha256"] == pinned.manifest["inputs"]["ledger_data"]["sha256"]
    assert reference["effect_watermark"] == str(pinned.manifest["effect_watermark"])
    assert reference["assignment_watermark"] == str(pinned.manifest["assignment_watermark"])
    assert records["observations"][0]["external_event_at"] is None
    assert records["payouts"][0]["gross_minor"] == "62"
    assert all(
        not key.endswith("_hex") for table in records.values() for row in table for key in row
    )
    import ledgerops.exchange_consumer as consumer

    standalone = tmp_path / "consumer.py"
    shutil.copyfile(consumer.__file__, standalone)
    finished = subprocess.run(
        [sys.executable, "-I", "-S", str(standalone), str(directory)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(finished.stdout) == summary
