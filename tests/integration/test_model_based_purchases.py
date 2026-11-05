"""Model-based testing: random command sequences against the reference model.

Hypothesis generates sequences of purchases, reversals, exact retries, reused
request keys, reused operation IDs, wrong actors and customer funding. Every
step goes to both the independent single-threaded oracle in
`reference/command_model.py` and the production admission functions on real
PostgreSQL. After each step the outcome, the posted balances and the journal
count must agree.

This follows the QuickCheck tradition of state-machine testing (Claessen and
Hughes; Claessen et al., "Finding Race Conditions in Erlang with QuickCheck and
PULSE", ICFP 2009): instead of hand-writing scenarios, generate them and let a
model decide what should have happened. It is sequential: concurrency stays in
the lock-forcing tests such as test_purchase_concurrency.py. Each example uses
a fresh workspace in the test database, so examples do not share state.

`LEDGEROPS_MODEL_EXAMPLES` and `LEDGEROPS_MODEL_STEPS` change the search size.
Under CI the search is derandomized so a run is reproducible.
"""

import copy
import os
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    invariant,
    rule,
    run_state_machine_as_test,
)

from ledgerops.db.admission import AdmissionError, Principal, submit_purchase, submit_reversal
from ledgerops.domain import (
    ActorId,
    ClientId,
    OperationId,
    PurchaseCommand,
    RequestKey,
    ReversalCommand,
    WorkspaceId,
)
from reference.command_model import CommandModel, ModelError
from tests.integration.helpers import account_id, seed_accounts
from tests.integration.purchase_helpers import fund_customer

pytestmark = pytest.mark.integration

CLIENT_ID = "60000000-0000-4000-8000-000000000001"
REASON = "customer_refund"


def _comparable_production(call):
    """Run a production admission call and reduce it to (status, code, operation, replayed)."""
    try:
        result = call()
    except AdmissionError as exc:
        return (exc.http_status, exc.code, None, False)
    body = result.body
    if "error" in body:
        error = body["error"]
        return (result.http_status, error["code"], error["operation_id"], result.replayed)
    return (result.http_status, body["effect"], body["operation_id"], result.replayed)


def _comparable_model(call):
    try:
        outcome = call()
    except ModelError as exc:
        return (_MODEL_ERROR_STATUS[exc.code], exc.code, None, False)
    return (outcome.http_status, outcome.code, outcome.operation_id, outcome.replayed)


# Unretained model errors and the production exceptions they correspond to.
_MODEL_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "BUSINESS_ID_CONFLICT": 409,
}


class PurchaseLedgerMachine(RuleBasedStateMachine):
    purchases = Bundle("purchases")

    def __init__(self, db, golden):
        super().__init__()
        self.db = db
        self.golden = copy.deepcopy(golden)
        workspace_id = str(uuid4())
        self.golden["workspace"]["id"] = workspace_id
        for row in self.golden["accounts"]:
            row["workspace_id"] = workspace_id
        self.golden["purchase_request"]["workspace_id"] = workspace_id
        seed_accounts(db, self.golden)

        self.workspace = WorkspaceId.parse(workspace_id)
        self.customer_account = account_id(self.golden, "customer")
        self.merchants = {
            "M1": account_id(self.golden, "M1"),
            "M2": account_id(self.golden, "M2"),
        }
        self.owner = {row["id"]: row["owner_id"] for row in self.golden["accounts"]}
        self.model = CommandModel(workspace_id, self.golden["accounts"])
        self.steps = 0

    # ----- helpers -----------------------------------------------------------

    def _purchase(self, *, operation_id, key, merchant, amount, actor=None):
        actor = actor or self.owner[self.customer_account]
        merchant_account = self.merchants[merchant]
        model = _comparable_model(
            lambda: self.model.submit(
                {
                    "kind": "PURCHASE",
                    "workspace_id": str(self.workspace),
                    "client_id": CLIENT_ID,
                    "actor_id": actor,
                    "key": key,
                    "operation_id": operation_id,
                    "customer_account_id": self.customer_account,
                    "merchant_account_id": merchant_account,
                    "amount_minor": amount,
                }
            )
        )
        principal = Principal(self.workspace, ClientId.parse(CLIENT_ID), ActorId.parse(actor))
        command = PurchaseCommand.from_payload(
            self.workspace,
            {
                "operation_id": operation_id,
                "customer_account_id": self.customer_account,
                "merchant_account_id": merchant_account,
                "amount_minor": str(amount),
                "currency": "PLN",
            },
        )
        request_key = RequestKey(self.workspace, principal.client_id, key)

        def production():
            with self.db.connect(role="app") as conn:
                return submit_purchase(conn, principal, command, request_key)

        actual = _comparable_production(production)
        assert actual == model, (
            f"purchase {operation_id} key={key}: production {actual} != model {model}"
        )
        self.steps += 1
        return actual

    def _reversal(self, *, operation_id, original, key, actor):
        model = _comparable_model(
            lambda: self.model.submit(
                {
                    "kind": "REVERSAL",
                    "workspace_id": str(self.workspace),
                    "client_id": CLIENT_ID,
                    "actor_id": actor,
                    "key": key,
                    "operation_id": operation_id,
                    "original_operation_id": original,
                    "reason": REASON,
                }
            )
        )
        principal = Principal(self.workspace, ClientId.parse(CLIENT_ID), ActorId.parse(actor))
        command = ReversalCommand(
            self.workspace,
            OperationId.parse(operation_id),
            OperationId.parse(original),
            REASON,
        )
        request_key = RequestKey(self.workspace, principal.client_id, key, "reversal:v1")

        def production():
            with self.db.connect(role="app") as conn:
                return submit_reversal(conn, principal, command, request_key)

        actual = _comparable_production(production)
        assert actual == model, (
            f"reversal {operation_id} of {original}: production {actual} != model {model}"
        )
        self.steps += 1

    # ----- rules -------------------------------------------------------------

    @rule(amount=st.integers(min_value=1, max_value=150))
    def fund_customer_account(self, amount):
        """Explicit setup money: an OPENING journal in both worlds."""
        operation_id = str(uuid4())
        fund_customer(self.db, self.golden, amount, customer_account_id=self.customer_account)
        self.model.submit(
            {
                "kind": "OPENING",
                "operation_id": operation_id,
                "amount_minor": amount,
                "credit_account_id": self.customer_account,
            }
        )

    @rule(
        target=purchases,
        merchant=st.sampled_from(["M1", "M2"]),
        amount=st.integers(min_value=1, max_value=120),
    )
    def new_purchase(self, merchant, amount):
        operation_id, key = str(uuid4()), f"purchase-{uuid4().hex[:12]}"
        self._purchase(operation_id=operation_id, key=key, merchant=merchant, amount=amount)
        return (operation_id, key, merchant, amount)

    @rule(purchase=purchases)
    def retry_same_purchase(self, purchase):
        """A client retry after a timeout: same key, same operation, same payload."""
        operation_id, key, merchant, amount = purchase
        status, _, _, replayed = self._purchase(
            operation_id=operation_id, key=key, merchant=merchant, amount=amount
        )
        assert replayed or status in {404, 409}

    @rule(purchase=purchases, amount=st.integers(min_value=1, max_value=120))
    def reuse_key_for_different_purchase(self, purchase, amount):
        _, key, merchant, original_amount = purchase
        if amount == original_amount:
            amount += 1
        self._purchase(operation_id=str(uuid4()), key=key, merchant=merchant, amount=amount)

    @rule(purchase=purchases, amount=st.integers(min_value=1, max_value=120))
    def reuse_operation_id_with_new_key(self, purchase, amount):
        operation_id, _, merchant, _ = purchase
        self._purchase(
            operation_id=operation_id,
            key=f"purchase-{uuid4().hex[:12]}",
            merchant=merchant,
            amount=amount,
        )

    @rule(purchase=purchases, merchant=st.sampled_from(["M1", "M2"]))
    def purchase_by_wrong_actor(self, purchase, merchant):
        """A merchant tries to spend the customer's account."""
        self._purchase(
            operation_id=str(uuid4()),
            key=f"purchase-{uuid4().hex[:12]}",
            merchant=merchant,
            amount=1,
            actor=self.owner[self.merchants[merchant]],
        )

    @rule(purchase=purchases)
    def reverse_purchase(self, purchase):
        original, _, merchant, _ = purchase
        self._reversal(
            operation_id=str(uuid4()),
            original=original,
            key=f"reversal-{uuid4().hex[:12]}",
            actor=self.owner[self.merchants[merchant]],
        )

    @rule(purchase=consumes(purchases))
    def reverse_purchase_and_retry(self, purchase):
        original, _, merchant, _ = purchase
        operation_id, key = str(uuid4()), f"reversal-{uuid4().hex[:12]}"
        actor = self.owner[self.merchants[merchant]]
        self._reversal(operation_id=operation_id, original=original, key=key, actor=actor)
        self._reversal(operation_id=operation_id, original=original, key=key, actor=actor)

    @rule(purchase=purchases)
    def reversal_by_customer_is_not_found(self, purchase):
        original, _, _, _ = purchase
        self._reversal(
            operation_id=str(uuid4()),
            original=original,
            key=f"reversal-{uuid4().hex[:12]}",
            actor=self.owner[self.customer_account],
        )

    # ----- invariants --------------------------------------------------------

    @invariant()
    def balances_and_journals_match_the_model(self):
        with self.db.connect(role="app") as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute(
                """SELECT account_id::text, debits, credits, booked, reserved
                   FROM ledger.account_balance WHERE workspace_id=%s""",
                (str(self.workspace),),
            ).fetchall()
            journals = conn.execute(
                "SELECT count(*) FROM ledger.journal WHERE workspace_id=%s",
                (str(self.workspace),),
            ).fetchone()
        actual = {
            _value(row, "account_id", 0): tuple(
                int(_value(row, name, index + 1))
                for index, name in enumerate(("debits", "credits", "booked", "reserved"))
            )
            for row in rows
        }
        expected = {
            account: (row["debits"], row["credits"], row["booked"], row["reserved"])
            for account, row in self.model.balances.items()
        }
        assert actual == expected
        assert int(_value(journals, "count", 0)) == len(self.model.journals)


def _value(row, name, index):
    return row[name] if isinstance(row, dict) else row[index]


def test_random_purchase_and_reversal_sequences_match_the_reference_model(db, golden):
    examples = int(os.environ.get("LEDGEROPS_MODEL_EXAMPLES", "20"))
    steps = int(os.environ.get("LEDGEROPS_MODEL_STEPS", "25"))
    run_state_machine_as_test(
        lambda: PurchaseLedgerMachine(db, golden),
        settings=settings(
            max_examples=examples,
            stateful_step_count=steps,
            deadline=None,
            derandomize=bool(os.environ.get("CI")),
            database=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
