"""Literal benchmark amounts remain independently checkable when scaling identities."""

import pytest

from ledgerops.export import load_export_bundle, publish_snapshot
from ledgerops.reconciliation import StatementTrust, reconcile_inputs
from tests.benchmark.workload import CLEARING, PROVIDER, SECRET, workload


@pytest.mark.parametrize("skew", [False, True])
def test_literal_scale_preserves38_per_unit_and_full_group_coverage(tmp_path, skew):
    data, statement = workload(8, skew=skew)
    clearing = next(row for row in data["balances"] if row["account_id"] == CLEARING)
    assert (clearing["debits"], clearing["credits"], clearing["booked"]) == ("800", "496", "304")
    bundle = load_export_bundle(publish_snapshot(data, tmp_path, code_revision="a" * 40))
    report = reconcile_inputs(
        bundle,
        statement,
        trust=StatementTrust(PROVIDER, {"bench-v1": SECRET}),
        code_revision="a" * 40,
    )
    assert report["reconciled"] and len(report["matched_provider_keys"]) == 8
    assert len(report["groups"]) == (1 if skew else 8)
    assert sum(int(group["gross_minor"]) for group in report["groups"]) == 496
