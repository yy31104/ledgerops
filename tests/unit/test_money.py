from decimal import Decimal

import pytest

from ledgerops.domain.money import (
    AmountMinor,
    checked_add,
    checked_operation_total,
    checked_total,
    parse_fee,
)


@pytest.mark.parametrize("value", ["1", "80", "9000000000000000"])
def test_amount_accepts_positive_canonical_minor_units(value):
    assert AmountMinor.parse(value).value == int(value)


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "+1",
        "01",
        "00",
        "1.0",
        "1e2",
        " 1",
        "1 ",
        "1\n",
        "١",
        "１",
        "",
        "9000000000000001",
        "9" * 5000,
    ],
)
def test_amount_rejects_noncanonical_or_out_of_range_text(value):
    with pytest.raises(ValueError):
        AmountMinor.parse(value)


@pytest.mark.parametrize("value", [True, False, 80, 80.0, Decimal("80"), None, b"80"])
def test_wire_amount_rejects_every_non_string_numeric_representation(value):
    with pytest.raises(TypeError):
        AmountMinor.parse(value)


@pytest.mark.parametrize("value", [True, False, 1.0, Decimal("1"), "1"])
def test_domain_amount_requires_actual_integer(value):
    with pytest.raises(TypeError):
        AmountMinor(value)


@pytest.mark.parametrize("value", [0, -1, 9_000_000_000_000_001])
def test_domain_amount_constructor_cannot_bypass_bounds(value):
    with pytest.raises(ValueError):
        AmountMinor(value)


def test_fee_permits_zero_and_operation_total_checks_its_own_bound():
    assert parse_fee("0") == 0
    assert parse_fee("9000000000000000") == 9_000_000_000_000_000
    assert checked_operation_total(AmountMinor(1), 8_999_999_999_999_999) == 9_000_000_000_000_000
    with pytest.raises(ValueError):
        checked_operation_total(AmountMinor(1), 9_000_000_000_000_000)
    with pytest.raises(TypeError):
        checked_operation_total(AmountMinor(1), True)
    with pytest.raises(ValueError):
        parse_fee("-1")


def test_aggregate_numeric_38_boundary_is_checked_without_float_conversion():
    limit = 99_999_999_999_999_999_999_999_999_999_999_999_999
    assert checked_total(0) == 0
    assert checked_total(limit) == limit
    assert checked_add(limit - 1, 1) == limit
    with pytest.raises(ValueError):
        checked_add(limit, 1)
    with pytest.raises(ValueError):
        checked_total(-1)
    # Invalid inputs cannot cancel each other and pass only a final sum check.
    with pytest.raises(ValueError):
        checked_add(-1, 1)
    with pytest.raises(TypeError):
        checked_total(True)
