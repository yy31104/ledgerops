"""Small integer oracle over raw rows, independent of production money and SQL code.

Rows may carry UUID objects or strings. PostgreSQL numeric projection values may
be Decimal. This R1 oracle handles posted balances only; it does not verify holds.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

Row = Mapping[str, Any]
AccountKey = tuple[str, str]


@dataclass(frozen=True)
class Balance:
    debits: int
    credits: int
    booked: int


def _key(row: Row, id_field: str) -> AccountKey:
    return str(row["workspace_id"]), str(row[id_field])


def _integer(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    raise AssertionError(f"Expected exact integer, got {value!r}")


def _index(rows: Iterable[Row], id_field: str) -> dict[AccountKey, Row]:
    result = {}
    for row in rows:
        key = _key(row, id_field)
        assert key not in result, f"Duplicate {id_field}: {key}"
        result[key] = row
    return result


def fold_entries(accounts: Iterable[Row], entries: Iterable[Row]) -> dict[AccountKey, Balance]:
    """Recompute all accounts, including idle ones, and reject unbalanced journals.

    accounts: workspace_id, id, normal_side (D/C).
    entries: workspace_id, journal_id, line_no, account_id, side, amount_minor.
    Missing/empty journal headers require the separate database/header checks;
    they cannot be discovered from an entry-only export.
    """
    account_index = _index(accounts, "id")
    totals = {key: {"D": 0, "C": 0} for key in account_index}
    journal_totals = defaultdict(lambda: {"D": 0, "C": 0})
    seen_lines = set()
    for row in entries:
        account_key = _key(row, "account_id")
        journal_key = _key(row, "journal_id")
        line_key = (*journal_key, row["line_no"])
        assert account_key in account_index, f"Unknown account: {account_key}"
        assert line_key not in seen_lines, f"Duplicate journal line: {line_key}"
        seen_lines.add(line_key)
        side = row["side"]
        assert side in ("D", "C"), f"Invalid entry side: {side!r}"
        amount = _integer(row["amount_minor"])
        assert amount > 0, f"Nonpositive journal amount: {line_key}"
        totals[account_key][side] += amount
        journal_totals[journal_key][side] += amount
    for key, total in journal_totals.items():
        assert total["D"] == total["C"], f"Unbalanced journal {key}: {total}"

    result = {}
    for key, account in account_index.items():
        side = account["normal_side"]
        assert side in ("D", "C"), f"Invalid normal side for {key}: {side!r}"
        debits, credits = totals[key]["D"], totals[key]["C"]
        booked = debits - credits if side == "D" else credits - debits
        result[key] = Balance(debits, credits, booked)
    return result


def assert_projection_matches(
    accounts: Iterable[Row], entries: Iterable[Row], projections: Iterable[Row]
) -> None:
    """Raise with the account and differing fields, including missing/extra rows."""
    expected = fold_entries(accounts, entries)
    actual = _index(projections, "account_id")
    assert actual.keys() == expected.keys(), (
        f"Projection account set mismatch: missing={expected.keys() - actual.keys()}, "
        f"extra={actual.keys() - expected.keys()}"
    )
    for key, balance in expected.items():
        differences = {
            field: (_integer(actual[key][field]), getattr(balance, field))
            for field in ("debits", "credits", "booked")
            if _integer(actual[key][field]) != getattr(balance, field)
        }
        assert not differences, f"Projection mismatch for {key} (actual, expected): {differences}"


def assert_purchase_matches(
    request: Row,
    accounts: Iterable[Row],
    journals: Iterable[Row],
    entries: Iterable[Row],
    before: Iterable[Row],
    after: Iterable[Row],
) -> None:
    """Check an isolated purchase against the ORIGINAL frozen business request.

    request fields: workspace_id, operation_id, customer_id, customer_account_id,
    merchant_id, merchant_account_id, amount_minor. Snapshots span this purchase
    only and include all accounts, so an unrelated projection delta also fails.
    """
    workspace_id = str(request["workspace_id"])
    account_index = _index(accounts, "id")
    customer = workspace_id, str(request["customer_account_id"])
    merchant = workspace_id, str(request["merchant_account_id"])
    for key, owner_field, account_class in (
        (customer, "customer_id", "CUSTOMER_LIABILITY"),
        (merchant, "merchant_id", "MERCHANT_LIABILITY"),
    ):
        assert key in account_index, f"Requested account missing: {key}"
        account = account_index[key]
        assert str(account["owner_id"]) == str(request[owner_field]), (
            f"Requested owner mismatch for {key}"
        )
        assert account["class"] == account_class and account["normal_side"] == "C", (
            f"Requested account class/normal side mismatch: {key}"
        )
    matching = [
        journal
        for journal in journals
        if str(journal["workspace_id"]) == workspace_id
        and str(journal["operation_id"]) == str(request["operation_id"])
    ]
    assert len(matching) == 1, f"Expected one purchase journal, got {len(matching)}"
    journal_key = _key(matching[0], "journal_id")
    actual_lines = [
        (str(row["account_id"]), row["side"], _integer(row["amount_minor"]))
        for row in entries
        if _key(row, "journal_id") == journal_key
    ]
    amount = _integer(request["amount_minor"])
    assert amount > 0 and customer != merchant, "Invalid frozen purchase request"
    expected_lines = [(customer[1], "D", amount), (merchant[1], "C", amount)]
    assert sorted(actual_lines) == sorted(expected_lines), (
        f"Purchase owner/amount mismatch: expected {expected_lines}, actual {actual_lines}"
    )
    before_index, after_index = _index(before, "account_id"), _index(after, "account_id")
    assert before_index.keys() == after_index.keys() == account_index.keys(), (
        "Purchase projection snapshot account set mismatch"
    )
    for key in account_index:
        expected_delta = (0, 0, 0)
        if key == customer:
            expected_delta = (amount, 0, -amount)
        elif key == merchant:
            expected_delta = (0, amount, amount)
        actual_delta = tuple(
            _integer(after_index[key][field]) - _integer(before_index[key][field])
            for field in ("debits", "credits", "booked")
        )
        assert actual_delta == expected_delta, (
            f"Purchase projection delta mismatch for {key}: "
            f"expected {expected_delta}, actual {actual_delta}"
        )
