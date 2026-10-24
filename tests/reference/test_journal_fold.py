"""Frozen business expectations and explicit, isolated reference-oracle mutants."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from reference.journal_fold import (
    Balance,
    assert_projection_matches,
    assert_purchase_matches,
    fold_entries,
)

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "fixtures/golden/purchase_80.json"


@pytest.fixture
def golden():
    return json.loads(GOLDEN_PATH.read_text())


def check_purchase(golden):
    assert_purchase_matches(
        golden["purchase_request"],
        golden["accounts"],
        golden["journals"],
        golden["entries"],
        golden["before_purchase"],
        golden["after_purchase"],
    )


def test_hand_calculated_opening_and_purchase_include_idle_merchant(golden):
    folded = fold_entries(golden["accounts"], golden["entries"])
    assert list(folded.values()) == [
        Balance(100, 0, 100),
        Balance(80, 100, 20),
        Balance(0, 80, 80),
        Balance(0, 0, 0),
    ]
    assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])
    check_purchase(golden)


def test_missed_customer_projection_update_is_located_by_fold(golden):
    # T9: valid balanced rows, merchant updated, customer left at opening state.
    golden["after_purchase"][1] = deepcopy(golden["before_purchase"][1])
    assert golden["after_purchase"][1]["booked"] == 100
    with pytest.raises(AssertionError, match=r"Projection mismatch.*000002.*debits.*booked"):
        assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])


def test_consistent_wrong_merchant_passes_fold_but_original_request_catches_it(golden):
    original_request = deepcopy(golden["purchase_request"])
    m2 = golden["accounts"][3]["id"]
    golden["entries"][3]["account_id"] = m2
    golden["after_purchase"][2].update(credits=0, booked=0, version=0)
    golden["after_purchase"][3].update(credits=80, booked=80, version=1)
    # This positive control proves a balanced and internally consistent mutation.
    assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])
    assert golden["purchase_request"] == original_request
    with pytest.raises(AssertionError, match="Purchase owner/amount mismatch"):
        check_purchase(golden)


def test_correct_journal_with_wrong_projection_is_rejected(golden):
    golden["after_purchase"][2].update(credits=0, booked=0)
    golden["after_purchase"][3].update(credits=80, booked=80)
    with pytest.raises(AssertionError, match=r"Purchase projection delta mismatch.*000003"):
        check_purchase(golden)


def test_balanced_wrong_amount_fails_request_assertion(golden):
    golden["entries"][2]["amount_minor"] = 79
    golden["entries"][3]["amount_minor"] = 79
    golden["after_purchase"][1].update(debits=79, booked=21)
    golden["after_purchase"][2].update(credits=79, booked=79)
    assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])
    with pytest.raises(AssertionError, match="Purchase owner/amount mismatch"):
        check_purchase(golden)


def test_debit_normal_account_subtracts_credits(golden):
    # Separate literal balanced journal: customer liability pays 15 to clearing.
    extra = deepcopy(golden["entries"][2:])
    for entry in extra:
        entry["journal_id"] = "50000000-0000-4000-8000-000000000003"
        entry["amount_minor"] = 15
    extra[1]["account_id"] = golden["accounts"][0]["id"]
    folded = fold_entries(golden["accounts"], golden["entries"] + extra)
    assert list(folded.values()) == [
        Balance(100, 15, 85),
        Balance(95, 100, 5),
        Balance(0, 80, 80),
        Balance(0, 0, 0),
    ]


def test_unbalanced_journal_rejected_even_when_other_journal_cancels_total(golden):
    golden["entries"][0]["amount_minor"] = 101
    golden["entries"][3]["amount_minor"] = 81
    with pytest.raises(AssertionError, match="Unbalanced journal"):
        fold_entries(golden["accounts"], golden["entries"])


def test_no_entries_still_returns_every_zero_account(golden):
    assert list(fold_entries(golden["accounts"], []).values()) == [Balance(0, 0, 0)] * 4


def test_missing_zero_activity_projection_rejected(golden):
    golden["after_purchase"].pop()
    with pytest.raises(AssertionError, match=r"Projection account set mismatch.*000004"):
        assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])


def test_account_identity_includes_workspace(golden):
    golden["entries"][3]["workspace_id"] = "10000000-0000-4000-8000-000000000002"
    with pytest.raises(AssertionError, match="Unknown account"):
        fold_entries(golden["accounts"], golden["entries"])


def test_raw_postgres_uuid_and_numeric_types(golden):
    for row in golden["after_purchase"]:
        row["workspace_id"] = UUID(row["workspace_id"])
        row["account_id"] = UUID(row["account_id"])
        for name in ("debits", "credits", "booked"):
            row[name] = Decimal(row[name])
    assert_projection_matches(golden["accounts"], golden["entries"], golden["after_purchase"])
    check_purchase(golden)


@pytest.mark.parametrize("invalid", [True, 1.5, Decimal("1.5"), Decimal("NaN")])
def test_inexact_entry_amounts_are_not_coerced(golden, invalid):
    golden["entries"][0]["amount_minor"] = invalid
    with pytest.raises(AssertionError, match="Expected exact integer"):
        fold_entries(golden["accounts"], golden["entries"])


def test_duplicate_exported_line_is_rejected(golden):
    golden["entries"].append(deepcopy(golden["entries"][0]))
    with pytest.raises(AssertionError, match="Duplicate journal line"):
        fold_entries(golden["accounts"], golden["entries"])


def test_correct_account_id_with_wrong_owner_is_rejected(golden):
    golden["accounts"][2]["owner_id"] = golden["accounts"][3]["owner_id"]
    with pytest.raises(AssertionError, match="Requested owner mismatch"):
        check_purchase(golden)


def test_operation_must_have_exactly_one_journal(golden):
    golden["journals"].append(deepcopy(golden["journals"][1]))
    with pytest.raises(AssertionError, match="Expected one purchase journal, got 2"):
        check_purchase(golden)
