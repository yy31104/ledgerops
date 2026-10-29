"""Unknown source capabilities must never fall back to legacy interpretation."""

from copy import deepcopy

import pytest

from ledgerops.reconciliation.publication import PublicationError, _input_refs


@pytest.mark.parametrize(
    "change",
    [
        {"source_kind": "FUTURE_UNSUPPORTED"},
        {"manifest_version": 2},
        {"manifest_version": True},
        {"manifest_version": "1"},
        {"source_kind": "FINANCIAL_STREAM10_COMPACT_V1", "manifest_version": 1},
        {"source_kind": "FINANCIAL_STREAM10_COMPACT_V1", "manifest_version": 2.0},
    ],
)
def test_unknown_or_wrong_typed_source_version_cannot_use_legacy_refs(change):
    manifest = {
        "manifest_version": 1,
        "inputs": {
            name: {"sha256": str(index) * 64, "bytes": 1}
            for index, name in enumerate(("ledger_manifest", "ledger_data", "statement"), 1)
        },
    }
    assert _input_refs(manifest) == manifest["inputs"]
    bad = deepcopy(manifest)
    bad.update(change)
    with pytest.raises(PublicationError, match="INVALID_INPUT_MANIFEST"):
        _input_refs(bad)
