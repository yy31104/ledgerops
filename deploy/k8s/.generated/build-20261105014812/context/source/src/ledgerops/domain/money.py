"""Synthetic PLN minor units. No float, Decimal conversion, or rounding."""

import re
from dataclasses import dataclass

MAX_AMOUNT_MINOR = 9_000_000_000_000_000
MAX_TOTAL_MINOR = 10**38 - 1
_INTEGER_TEXT = re.compile(r"0|[1-9][0-9]*", flags=re.ASCII)


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int:
    # bool is an int subclass and must not enter the monetary domain.
    if type(value) is not int:
        raise TypeError("minor units must be an integer, not bool or float")
    if not minimum <= value <= maximum:
        raise ValueError(f"minor units must be between {minimum} and {maximum}")
    return value


def _parse_minor_units(value: object, *, minimum: int) -> int:
    if not isinstance(value, str):
        raise TypeError("wire amounts must be canonical decimal strings")
    if not _INTEGER_TEXT.fullmatch(value):
        raise ValueError("amount must use unsigned canonical decimal digits")
    # Bound the text before int() so huge inputs do not depend on Python's
    # configurable integer-string conversion limit.
    if len(value) > 16:
        raise ValueError("amount exceeds the operation limit")
    return _bounded_integer(int(value), minimum=minimum, maximum=MAX_AMOUNT_MINOR)


@dataclass(frozen=True, slots=True)
class AmountMinor:
    value: int

    def __post_init__(self) -> None:
        _bounded_integer(self.value, minimum=1, maximum=MAX_AMOUNT_MINOR)

    @classmethod
    def parse(cls, value: object) -> "AmountMinor":
        return cls(_parse_minor_units(value, minimum=1))


def parse_fee(value: object) -> int:
    """Fees permit zero; wire purchase amounts do not."""
    return _parse_minor_units(value, minimum=0)


def checked_operation_total(amount: AmountMinor, fee: int) -> int:
    if type(amount) is not AmountMinor:
        raise TypeError("amount must be AmountMinor")
    _bounded_integer(fee, minimum=0, maximum=MAX_AMOUNT_MINOR)
    return _bounded_integer(amount.value + fee, minimum=1, maximum=MAX_AMOUNT_MINOR)


def checked_total(value: object) -> int:
    """Validate a nonnegative numeric(38,0) debit, credit, or reserved total."""
    return _bounded_integer(value, minimum=0, maximum=MAX_TOTAL_MINOR)


def checked_add(total: int, delta: int) -> int:
    """Add nonnegative totals without silently exceeding numeric(38,0)."""
    return checked_total(checked_total(total) + checked_total(delta))
