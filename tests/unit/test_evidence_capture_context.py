"""R6 must not label barrier/READ COMMITTED previews as RR source selection."""

from contextlib import contextmanager

import pytest
from test_verification import snapshot as snapshot

from ledgerops import exchange, stream_verification
from ledgerops.exchange_consumer import ExchangeError
from tests.unit.stream_fixture_helpers import source, write

CONTEXT = "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1"


@pytest.mark.parametrize("entry", ["build", "publish"])
def test_exchange_rejects_declared_barrier_source_before_claiming_rr(
    snapshot, tmp_path, monkeypatch, entry
):
    data = source(snapshot)
    selected = next(row["journal_id"] for row in data["journals"] if row["kind"] == "PURCHASE")
    stream = write(data, tmp_path / "input")
    original = stream_verification.stage_stream

    @contextmanager
    def barrier_source(*args, **kwargs):
        with original(*args, **kwargs) as staged:
            staged.source = {**staged.source, "capture_context": CONTEXT}
            yield staged

    monkeypatch.setattr(stream_verification, "stage_stream", barrier_source)
    with pytest.raises(ExchangeError, match="EXCHANGE_REQUIRES_REPEATABLE_READ_SOURCE"):
        if entry == "build":
            exchange.build_stream_exchange(stream, journal_ids=[selected])
        else:
            exchange.publish_stream_exchange(stream, tmp_path / "output", journal_ids=[selected])
    assert not (tmp_path / "output").exists()


def test_original_rr_stream_exchange_stays_minor1_without_context_change(snapshot, tmp_path):
    data = source(snapshot)
    selected = next(row["journal_id"] for row in data["journals"] if row["kind"] == "PURCHASE")
    stream = write(data, tmp_path / "input")
    manifest_raw, _ = exchange.build_stream_exchange(stream, journal_ids=[selected])
    manifest = exchange.decode(manifest_raw)
    assert manifest["schema_minor"] == 1
    assert manifest["selection"]["cutoff"] == "SOURCE_REPEATABLE_READ_SNAPSHOT_MEMBERSHIP"
