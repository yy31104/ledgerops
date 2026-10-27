"""Small semantic command oracle, independent of all production helpers.

This single-threaded model is test evidence, not an admission implementation. It
models retained decisions and exact posted money. Fixture OPENING/RESERVE events
are explicit setup, not a funding service or reservation lifecycle. Wire parsing,
SQL, transaction versions, permissions and concurrent scheduling are not modeled.
"""

from copy import deepcopy
from dataclasses import dataclass, replace

_OPERATION_LIMIT = 9_000_000_000_000_000
_TOTAL_LIMIT = 10**38 - 1


class ModelError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Outcome:
    http_status: int
    code: str
    operation_id: str
    journal_id: str | None = None
    original_operation_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class Posting:
    account_id: str
    side: str
    amount_minor: int


@dataclass(frozen=True)
class Journal:
    operation_id: str
    kind: str
    entries: tuple[Posting, Posting]


@dataclass(frozen=True)
class Retained:
    command: dict
    outcome: Outcome


def _amount(value, *, allow_zero=False, maximum=_OPERATION_LIMIT):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= maximum:
        raise ModelError("INVALID_REQUEST")
    return value


class CommandModel:
    """One workspace, immutable identity maps, retained results and append-only journals."""

    def __init__(self, workspace_id: str, accounts: list[dict]):
        self.workspace_id = workspace_id
        self.accounts = {row["id"]: deepcopy(row) for row in accounts}
        if len(self.accounts) != len(accounts):
            raise ModelError("DUPLICATE_ACCOUNT")
        self._totals = {key: {"D": 0, "C": 0, "reserved": 0} for key in self.accounts}
        self.operations: dict[str, Retained] = {}
        self.requests: dict[tuple[str, str, str], str] = {}
        self.journals: dict[str, Journal] = {}
        self.reversal_effects: dict[str, str] = {}
        for row in accounts:
            expected = "D" if row["class"] == "SYNTHETIC_CLEARING" else "C"
            if (
                row["class"]
                not in {"SYNTHETIC_CLEARING", "CUSTOMER_LIABILITY", "MERCHANT_LIABILITY"}
                or row["normal_side"] != expected
                or row.get("currency") != "PLN"
            ):
                raise ModelError("INVALID_ACCOUNT")

    @property
    def balances(self):
        result = {}
        for account_id, totals in self._totals.items():
            debits, credits = totals["D"], totals["C"]
            result[account_id] = {
                "debits": debits,
                "credits": credits,
                "booked": debits - credits
                if self.accounts[account_id]["normal_side"] == "D"
                else credits - debits,
                "reserved": totals["reserved"],
            }
        return result

    def _account(self, account_id, account_class):
        row = self.accounts.get(account_id)
        if row is None or row["class"] != account_class:
            raise ModelError("NOT_FOUND")
        return row

    def _authorize(self, command, actor_id):
        if command["kind"] == "PURCHASE":
            account = self._account(command["customer_account_id"], "CUSTOMER_LIABILITY")
        elif command["kind"] == "REVERSAL":
            original = self.operations.get(command["original_operation_id"])
            if (
                original is None
                or original.command["kind"] != "PURCHASE"
                or original.outcome.http_status != 201
            ):
                raise ModelError("NOT_FOUND")
            account = self._account(original.command["merchant_account_id"], "MERCHANT_LIABILITY")
        else:
            raise ModelError("NOT_FOUND")
        if account["owner_id"] != actor_id:
            raise ModelError("NOT_FOUND")

    def _post(self, operation_id, kind, entries):
        assert operation_id not in self.journals
        assert entries[0].side == "D" and entries[1].side == "C"
        assert entries[0].amount_minor == entries[1].amount_minor
        assert entries[0].account_id != entries[1].account_id
        for entry in entries:
            self._totals[entry.account_id][entry.side] += entry.amount_minor
        self.journals[operation_id] = Journal(operation_id, kind, entries)
        return f"journal:{operation_id}"

    def _fixture(self, event):
        if event["kind"] == "RESERVE":
            account = event["account_id"]
            self._account(account, "MERCHANT_LIABILITY")
            reserved = _amount(event["reserved_minor"], allow_zero=True, maximum=_TOTAL_LIMIT)
            if reserved > self.balances[account]["booked"]:
                raise ModelError("INVALID_RESERVATION_FIXTURE")
            self._totals[account]["reserved"] = reserved
            return None
        operation_id = event["operation_id"]
        if operation_id in self.operations:
            raise ModelError("DUPLICATE_FIXTURE_ID")
        amount = _amount(event["amount_minor"])
        target = event["credit_account_id"]
        row = self.accounts.get(target)
        clearing = [
            key for key, value in self.accounts.items() if value["class"] == "SYNTHETIC_CLEARING"
        ]
        if len(clearing) != 1 or row is None or row["normal_side"] != "C":
            raise ModelError("INVALID_OPENING_FIXTURE")
        if (
            self._totals[clearing[0]]["D"] + amount > _TOTAL_LIMIT
            or self._totals[target]["C"] + amount > _TOTAL_LIMIT
        ):
            raise ModelError("LIMIT_EXCEEDED")
        journal_id = self._post(
            operation_id,
            "OPENING",
            (Posting(clearing[0], "D", amount), Posting(target, "C", amount)),
        )
        result = Outcome(201, "opening_posted", operation_id, journal_id)
        self.operations[operation_id] = Retained(deepcopy(event), result)
        return result

    def submit(self, event: dict) -> Outcome | None:
        """Evaluate a semantic command; unretained errors leave all state unchanged."""
        kind = event.get("kind")
        if kind in {"OPENING", "RESERVE"}:
            return self._fixture(event)
        if kind not in {"PURCHASE", "REVERSAL"}:
            raise ModelError("INVALID_REQUEST")
        if event.get("workspace_id") != self.workspace_id:
            raise ModelError("NOT_FOUND")
        for name in ("client_id", "actor_id", "key", "operation_id"):
            if not isinstance(event.get(name), str) or not event[name]:
                raise ModelError("INVALID_REQUEST")
        command = {
            "kind": kind,
            "workspace_id": self.workspace_id,
            "operation_id": event["operation_id"],
        }
        if kind == "PURCHASE":
            command.update(
                customer_account_id=event["customer_account_id"],
                merchant_account_id=event["merchant_account_id"],
                amount_minor=_amount(event["amount_minor"]),
                currency=event.get("currency", "PLN"),
            )
            if command["currency"] != "PLN" or (
                command["customer_account_id"] == command["merchant_account_id"]
            ):
                raise ModelError("INVALID_REQUEST")
        else:
            reason = event["reason"]
            try:
                reason_size = len(reason.encode("utf-8"))
            except (AttributeError, UnicodeEncodeError) as exc:
                raise ModelError("INVALID_REQUEST") from exc
            if not 1 <= reason_size <= 256 or "\x00" in reason:
                raise ModelError("INVALID_REQUEST")
            command.update(original_operation_id=event["original_operation_id"], reason=reason)
        self._authorize(command, event["actor_id"])
        scope = event["client_id"], kind, event["key"]
        existing_id = self.requests.get(scope)
        if existing_id is not None:
            existing = self.operations[existing_id]
            self._authorize(existing.command, event["actor_id"])
            if existing.command != command:
                raise ModelError("IDEMPOTENCY_CONFLICT")
            return replace(existing.outcome, replayed=True)
        existing = self.operations.get(command["operation_id"])
        if existing is not None:
            self._authorize(existing.command, event["actor_id"])
            if existing.command != command:
                raise ModelError("BUSINESS_ID_CONFLICT")
            self.requests[scope] = command["operation_id"]
            return replace(existing.outcome, replayed=True)
        result = self._decide(command)
        self.operations[command["operation_id"]] = Retained(deepcopy(command), result)
        self.requests[scope] = command["operation_id"]
        return result

    def _decide(self, command):
        kind, operation_id = command["kind"], command["operation_id"]
        original_id = command.get("original_operation_id")
        if kind == "REVERSAL":
            if original_id in self.reversal_effects:
                return Outcome(
                    422, "ALREADY_REVERSED", operation_id, original_operation_id=original_id
                )
            original = self.operations[original_id].command
            debit, credit, amount = (
                original["merchant_account_id"],
                original["customer_account_id"],
                original["amount_minor"],
            )
        else:
            debit, credit, amount = (
                command["customer_account_id"],
                command["merchant_account_id"],
                command["amount_minor"],
            )
            self._account(credit, "MERCHANT_LIABILITY")
        for account_id in (debit, credit):
            if not self.accounts[account_id].get("enabled", True):
                raise ModelError("NOT_FOUND")
        balance = self.balances[debit]
        if balance["booked"] - balance["reserved"] < amount:
            return Outcome(
                422, "INSUFFICIENT_AVAILABLE", operation_id, original_operation_id=original_id
            )
        if (
            self._totals[debit]["D"] + amount > _TOTAL_LIMIT
            or self._totals[credit]["C"] + amount > _TOTAL_LIMIT
        ):
            return Outcome(422, "LIMIT_EXCEEDED", operation_id, original_operation_id=original_id)
        journal_id = self._post(
            operation_id, kind, (Posting(debit, "D", amount), Posting(credit, "C", amount))
        )
        if kind == "REVERSAL":
            self.reversal_effects[original_id] = operation_id
        return Outcome(
            201,
            "purchase_posted" if kind == "PURCHASE" else "purchase_reversed",
            operation_id,
            journal_id,
            original_id,
        )
