"""Whole-group and physical-row coverage tests independent of matching rules."""

from collections import Counter
from copy import deepcopy
from uuid import UUID

import pytest

from ledgerops.reconciliation.planning import build_plan

LEDGER = "a" * 64
STATEMENT = "b" * 64
MANIFEST = "c" * 64
WORKSPACE = str(UUID(int=1))
NAMESPACE = str(UUID(int=2))
MERCHANT = str(UUID(int=3))
BATCH = str(UUID(int=4))
ROW = str(UUID(int=5))


def identifier(number):
    return str(UUID(int=100 + number))


def key(number):
    return f"v1.{NAMESPACE}.{WORKSPACE}.{identifier(number)}"


def group(batch=BATCH):
    return {
        "namespace": NAMESPACE,
        "batch_id": batch,
        "currency": "PLN",
        "merchant_id": MERCHANT,
        "fee_rule_version": "quoted_fee_v1",
    }


def effect(number, *, batch=BATCH, row_id=ROW):
    return {
        "provider_key": key(number),
        "request": {
            "workspace_id": WORKSPACE,
            "operation_id": identifier(number),
            "merchant_id": MERCHANT,
            "currency": "PLN",
        },
        "assignment": {"batch_id": batch, "row_id": row_id, "fee_rule_version": "quoted_fee_v1"},
    }


def source_row(*numbers, batch=BATCH, row_id=ROW):
    return {
        "row_id": row_id,
        "group": group(batch),
        "provider_keys": [key(number) for number in numbers],
    }


def inputs():
    snapshot = {
        "format_version": 6,
        "schema_versions": ["schema.sql"],
        "server_version": 180006,
        "snapshot_witness": "1:2:",
        "workspace": {"id": WORKSPACE},
        "accounts": [{"id": MERCHANT}],
        "balances": [{"account_id": MERCHANT}],
        "operations": [],
        "payouts": [],
        "journals": [],
        "entries": [],
        "requests": [],
    }
    statement = {"namespace": NAMESPACE, "effects": [], "rows": []}
    return snapshot, statement


def plan(snapshot, statement, partitions=16, **digests):
    return build_plan(
        snapshot,
        statement,
        partitions=partitions,
        **{
            "ledger_sha256": LEDGER,
            "statement_sha256": STATEMENT,
            "manifest_sha256": MANIFEST,
            **digests,
        },
    )


def assert_coverage(result, expected):
    assert result["expected_coverage"] == sorted(expected)
    units = [item for unit in result["units"] for item in unit["coverage"]]
    partitions = [item for part in result["partitions"] for item in part["coverage"]]
    assert Counter(units) == Counter(expected)
    assert Counter(partitions) == Counter(expected)
    assert set(Counter(units).values()) == {1}
    assert Counter(unit for part in result["partitions"] for unit in part["unit_ids"]) == Counter(
        unit["unit_id"] for unit in result["units"]
    )


def expected_base(snapshot, statement):
    result = [
        f"{LEDGER}:ledger_metadata:0",
        f"{LEDGER}:workspace:0",
        f"{STATEMENT}:statement_metadata:0",
        f"{MANIFEST}:manifest_metadata:0",
    ]
    for table, rows in snapshot.items():
        if isinstance(rows, list) and table != "schema_versions":
            result.extend(f"{LEDGER}:{table}:{index}" for index in range(len(rows)))
    for table in ("effects", "rows"):
        result.extend(f"{STATEMENT}:{table}:{index}" for index in range(len(statement[table])))
    return result


def test_empty_inventory_has_one_global_unit_and_all_source_metadata():
    snapshot, statement = inputs()
    result = plan(snapshot, statement)
    assert len(result["units"]) == 1
    assert result["units"][0]["operation_ids"] == []
    assert result["units"][0]["group_keys"] == []
    assert len(result["partitions"]) == 16
    assert_coverage(result, expected_base(snapshot, statement))


def test_split_rows_same_group_and_ledger_dependencies_stay_together():
    snapshot, statement = inputs()
    journal = identifier(20)
    snapshot["operations"] = [{"operation_id": identifier(number)} for number in (1, 2)]
    snapshot["payouts"] = [{"operation_id": identifier(1), "provider_key": key(1)}]
    snapshot["journals"] = [{"operation_id": identifier(1), "journal_id": journal}]
    snapshot["entries"] = [
        {"journal_id": journal, "line_no": 1},
        {"journal_id": journal, "line_no": 2},
    ]
    snapshot["requests"] = [{"operation_id": identifier(2)}]
    snapshot["reservations"] = [{"payout_id": identifier(2)}]
    statement["effects"] = [effect(1), effect(2, row_id=identifier(21))]
    statement["rows"] = [source_row(1), source_row(2, row_id=identifier(21))]
    before = deepcopy((snapshot, statement))
    result = plan(snapshot, statement)
    assert (snapshot, statement) == before
    grouped = [unit for unit in result["units"] if unit["group_keys"]]
    assert len(grouped) == 1
    unit = grouped[0]
    assert unit["group_keys"] == [[NAMESPACE, BATCH, "PLN", MERCHANT, "quoted_fee_v1"]]
    assert unit["operation_ids"] == [identifier(1), identifier(2)]
    assert unit["effect_ordinals"] == [0, 1]
    assert unit["row_ordinals"] == [0, 1]
    assert f"{LEDGER}:entries:1" in unit["coverage"]
    assert f"{LEDGER}:reservations:0" in unit["coverage"]
    assert_coverage(result, expected_base(snapshot, statement))


def test_duplicate_key_across_groups_merges_whole_components_without_dropping_rows():
    snapshot, statement = inputs()
    other_batch = identifier(30)
    statement["effects"] = [
        effect(1),
        effect(2),
        effect(3, batch=other_batch, row_id=identifier(31)),
    ]
    statement["rows"] = [
        source_row(1, 2),
        source_row(1, 3, batch=other_batch, row_id=identifier(31)),
    ]
    result = plan(snapshot, statement)
    grouped = [unit for unit in result["units"] if unit["group_keys"]]
    assert len(grouped) == 1
    assert len(grouped[0]["group_keys"]) == 2
    assert grouped[0]["operation_ids"] == [identifier(1), identifier(2), identifier(3)]
    assert grouped[0]["effect_ordinals"] == [0, 1, 2]
    assert grouped[0]["row_ordinals"] == [0, 1]
    assert_coverage(result, expected_base(snapshot, statement))


def test_duplicate_effect_and_duplicate_row_identity_preserve_physical_multiplicity():
    snapshot, statement = inputs()
    statement["effects"] = [effect(1), effect(1), effect(2, batch=identifier(30))]
    statement["rows"] = [source_row(1), source_row(2, batch=identifier(30))]
    result = plan(snapshot, statement)
    unit = next(unit for unit in result["units"] if unit["effect_ordinals"])
    assert unit["effect_ordinals"] == [0, 1, 2]
    assert unit["row_ordinals"] == [0, 1]
    assert len(unit["group_keys"]) == 2
    assert_coverage(result, expected_base(snapshot, statement))


def test_unassigned_effect_local_only_payout_and_missing_journal_owner_are_not_dropped():
    snapshot, statement = inputs()
    unassigned = effect(1)
    unassigned["assignment"] = None
    statement["effects"] = [unassigned]
    snapshot["payouts"] = [{"operation_id": identifier(2), "provider_key": key(2)}]
    snapshot["journals"] = [{"operation_id": identifier(3), "journal_id": identifier(33)}]
    snapshot["entries"] = [{"journal_id": identifier(33)}, {"journal_id": identifier(34)}]
    result = plan(snapshot, statement)
    assert sorted(op for unit in result["units"] for op in unit["operation_ids"]) == [
        identifier(1),
        identifier(2),
        identifier(3),
    ]
    assert any(unit["coverage"] == [f"{LEDGER}:entries:1"] for unit in result["units"])
    assert_coverage(result, expected_base(snapshot, statement))


def test_null_group_and_members_get_explicit_inventory_units():
    snapshot, statement = inputs()
    statement["rows"] = [{"row_id": ROW, "group": None, "provider_keys": None}]
    result = plan(snapshot, statement)
    unit = next(unit for unit in result["units"] if unit["row_ordinals"])
    assert unit["group_keys"] == []
    assert unit["row_ordinals"] == [0]
    assert unit["coverage"] == [f"{STATEMENT}:rows:0"]
    assert_coverage(result, expected_base(snapshot, statement))


def test_reversal_original_is_reference_dependency_not_group_connector():
    snapshot, statement = inputs()
    snapshot["operations"] = [{"operation_id": identifier(1)}, {"operation_id": identifier(2)}]
    snapshot["reversal_attempts"] = [
        {"operation_id": identifier(2), "original_operation_id": identifier(1)}
    ]
    snapshot["reversal_effects"] = [
        {"reversal_operation_id": identifier(2), "original_operation_id": identifier(1)}
    ]
    result = plan(snapshot, statement)
    business = [unit for unit in result["units"] if unit["operation_ids"]]
    assert sorted(unit["operation_ids"] for unit in business) == [[identifier(1)], [identifier(2)]]
    reversal = next(unit for unit in business if unit["operation_ids"] == [identifier(2)])
    assert f"{LEDGER}:reversal_effects:0" in reversal["coverage"]
    assert_coverage(result, expected_base(snapshot, statement))


def test_duplicate_journal_identity_links_both_owners_instead_of_overwriting():
    snapshot, statement = inputs()
    snapshot["journals"] = [
        {"journal_id": ROW, "operation_id": identifier(1)},
        {"journal_id": ROW, "operation_id": identifier(2)},
    ]
    snapshot["entries"] = [{"journal_id": ROW}]
    result = plan(snapshot, statement)
    unit = next(unit for unit in result["units"] if unit["operation_ids"])
    assert unit["operation_ids"] == [identifier(1), identifier(2)]
    assert_coverage(result, expected_base(snapshot, statement))


def test_plan_is_deterministic_and_partition_count_does_not_change_semantic_units():
    snapshot, statement = inputs()
    for number in range(1, 10):
        statement["effects"].append(
            effect(number, batch=identifier(40 + number), row_id=identifier(50 + number))
        )
        statement["rows"].append(
            source_row(number, batch=identifier(40 + number), row_id=identifier(50 + number))
        )
    fixed = plan(snapshot, statement, partitions=16)
    assert plan(dict(reversed(list(snapshot.items()))), statement, partitions=16) == fixed
    for count in (1, 2, 4, 64):
        result = plan(snapshot, statement, partitions=count)
        assert result["units"] == fixed["units"]
        assert result["expected_coverage"] == fixed["expected_coverage"]
        assert len(result["partitions"]) == count
        assert_coverage(result, expected_base(snapshot, statement))


@pytest.mark.parametrize("count", [0, 65, True, "16", None])
def test_invalid_partition_count_fails(count):
    with pytest.raises(ValueError, match="INVALID_PARTITION_COUNT"):
        plan(*inputs(), partitions=count)


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, None, 12])
def test_input_digests_are_required(digest):
    with pytest.raises(ValueError, match="INVALID_PLAN_INPUT_DIGEST"):
        plan(*inputs(), ledger_sha256=digest)


def test_physical_row_bound_fails_without_partial_plan(monkeypatch):
    monkeypatch.setattr("ledgerops.reconciliation.planning.MAX_COVERAGE", 5)
    with pytest.raises(ValueError, match="PLAN_COVERAGE_LIMIT"):
        plan(*inputs())
