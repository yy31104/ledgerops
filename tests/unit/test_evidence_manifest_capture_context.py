"""Actual completed stream metadata cannot turn a barrier snapshot into RR evidence."""

import json

import pytest
from test_verification import snapshot as snapshot

from ledgerops import exchange, stream_verification
from ledgerops.exchange_consumer import ExchangeError
from ledgerops.export import _json_bytes
from tests.unit.stream_fixture_helpers import source, write

CONTEXT = "EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1"


def declare_context(path):
    manifest = json.loads((path / "manifest.json").read_bytes())
    manifest["source"]["capture_context"] = CONTEXT
    body = sum(row["bytes"] for row in manifest["chunks"])
    while True:
        raw = _json_bytes(manifest)
        if manifest["total_bytes"] == body + len(raw):
            break
        manifest["total_bytes"] = body + len(raw)
    (path / "manifest.json").write_bytes(raw)


@pytest.mark.parametrize("entry", ["build", "publish"])
def test_actual_barrier_context_manifest_is_readable_but_r6_cannot_label_it_rr(
    snapshot, tmp_path, entry
):
    data = source(snapshot)
    selected = next(row["journal_id"] for row in data["journals"] if row["kind"] == "PURCHASE")
    path = write(data, tmp_path / "source")
    declare_context(path)
    with stream_verification.stage_stream(path) as store:
        assert store.source["capture_context"] == CONTEXT
    with pytest.raises(ExchangeError, match="EXCHANGE_REQUIRES_REPEATABLE_READ_SOURCE"):
        if entry == "build":
            exchange.build_stream_exchange(path, journal_ids=[selected])
        else:
            exchange.publish_stream_exchange(path, tmp_path / "output", journal_ids=[selected])
    assert not (tmp_path / "output").exists()
