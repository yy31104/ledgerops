"""Frozen reversal semantic traces; production reversal is not implemented by these tests."""

import ast
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from reference.command_model import CommandModel, ModelError

CORPUS = Path(__file__).resolve().parents[2] / "fixtures/golden/r2_commands.json"


@pytest.fixture
def corpus():
    return json.loads(CORPUS.read_text())


def model_for(corpus):
    return CommandModel(corpus["workspace_id"], corpus["accounts"])


def state(model):
    return deepcopy(
        (model.balances, model.operations, model.requests, model.journals, model.reversal_effects)
    )


def assert_step(model, result, expected):
    assert (result.http_status if result else None) == expected["http_status"]
    assert (result.code if result else None) == expected["code"]
    assert (result.replayed if result else False) == expected["replayed"]
    balances = model.balances
    assert {key: row["booked"] for key, row in balances.items()} == expected["booked"]
    assert {key: row["reserved"] for key, row in balances.items()} == expected["reserved"]
    assert len(model.operations) == expected["operations"]
    assert len(model.requests) == expected["requests"]
    assert len(model.journals) == expected["journals"]
    assert model.reversal_effects == expected["reversal_effects"]


def run_prefix(corpus, count):
    model = model_for(corpus)
    for step in corpus["steps"][:count]:
        result = model.submit(step["command"])
        assert_step(model, result, step["expected"])
    return model


def test_frozen_rejected_funded_new_success_trace_and_original_history_unchanged(corpus):
    model = model_for(corpus)
    original_purchase = None
    original_result = None
    rejected = None
    for index, step in enumerate(corpus["steps"]):
        result = model.submit(step["command"])
        assert_step(model, result, step["expected"])
        if index == 1:
            original_purchase = model.journals["P1"]
            original_result = model.operations["P1"].outcome
        if index == 4:
            rejected = model.operations["RA"].outcome
            assert "RA" not in model.journals
            assert model.reversal_effects == {}
        if index >= 7:
            assert model.journals["P1"] == original_purchase
            assert model.operations["P1"].outcome == original_result
            assert model.operations["RA"].outcome == rejected
            assert "RA" not in model.journals and "RC" not in model.journals
    assert model.balances == corpus["expected_final_totals"]
    assert model.reversal_effects == {"P1": "RB"}
    reversal = model.journals["RB"]
    assert [(line.account_id, line.side, line.amount_minor) for line in reversal.entries] == [
        ("M1", "D", 80),
        ("C", "C", 80),
    ]
    # Reversal adds to cumulative totals rather than deleting the original entry.
    assert model.balances["C"]["debits"] == model.balances["M1"]["debits"] == 80
    assert model.balances["CL"]["booked"] == 200
    assert model.balances["C"]["booked"] + model.balances["M1"]["booked"] == 200


@pytest.mark.parametrize("phase", [5, 8, 9])
def test_new_transport_key_replays_original_reversal_success_or_rejection(corpus, phase):
    model = run_prefix(corpus, phase)
    original = corpus["steps"][phase - 1]["command"]
    saved = model.operations[original["operation_id"]].outcome
    before = state(model)
    result = model.submit({**original, "key": "extra-transport-key"})
    assert result == replace(saved, replayed=True)
    after = state(model)
    assert after[0] == before[0] and after[1] == before[1]
    assert len(after[2]) == len(before[2]) + 1
    assert after[3:] == before[3:]


@pytest.mark.parametrize(
    "changes", [{"actor_id": "CUSTOMER"}, {"actor_id": "MERCHANT-2"}, {"workspace_id": "OTHER"}]
)
def test_wrong_actor_or_scope_cannot_reverse_known_purchase(corpus, changes):
    model = run_prefix(corpus, 2)
    reversal = corpus["steps"][4]["command"]
    before = state(model)
    with pytest.raises(ModelError, match="^NOT_FOUND$"):
        model.submit({**reversal, **changes})
    assert state(model) == before


@pytest.mark.parametrize("original_id", ["O0", "ABSENT", "RA"])
def test_only_successful_purchase_is_an_eligible_original(corpus, original_id):
    model = run_prefix(corpus, 5)
    reversal = corpus["steps"][4]["command"]
    before = state(model)
    with pytest.raises(ModelError, match="^NOT_FOUND$"):
        model.submit(
            {
                **reversal,
                "key": "not-a-purchase",
                "operation_id": "R-INVALID",
                "original_operation_id": original_id,
            }
        )
    assert state(model) == before


def test_rejected_purchase_cannot_be_reversed(corpus):
    model = run_prefix(corpus, 1)
    purchase = {**corpus["steps"][1]["command"], "amount_minor": 101}
    assert model.submit(purchase).http_status == 422
    before = state(model)
    with pytest.raises(ModelError, match="^NOT_FOUND$"):
        model.submit(corpus["steps"][4]["command"])
    assert state(model) == before


@pytest.mark.parametrize(
    "key,code", [("P1-KEY", "IDEMPOTENCY_CONFLICT"), ("fresh-key", "BUSINESS_ID_CONFLICT")]
)
def test_changed_purchase_content_under_identity_is_unretained_conflict(corpus, key, code):
    model = run_prefix(corpus, 2)
    purchase = {**corpus["steps"][1]["command"], "key": key, "amount_minor": 81}
    before = state(model)
    with pytest.raises(ModelError, match=f"^{code}$"):
        model.submit(purchase)
    assert state(model) == before


@pytest.mark.parametrize(
    "key,code", [("RA-KEY", "IDEMPOTENCY_CONFLICT"), ("fresh-key", "BUSINESS_ID_CONFLICT")]
)
def test_changed_reversal_reason_is_unretained_conflict(corpus, key, code):
    model = run_prefix(corpus, 5)
    reversal = {**corpus["steps"][4]["command"], "key": key, "reason": "different_reason"}
    before = state(model)
    with pytest.raises(ModelError, match=f"^{code}$"):
        model.submit(reversal)
    assert state(model) == before


def test_rejected_purchase_stays_rejected_after_funding_but_new_business_can_succeed(corpus):
    model = run_prefix(corpus, 1)
    purchase = {**corpus["steps"][1]["command"], "amount_minor": 101}
    denied = model.submit(purchase)
    assert (denied.http_status, denied.code) == (422, "INSUFFICIENT_AVAILABLE")
    model.submit(
        {"kind": "OPENING", "operation_id": "EXTRA", "credit_account_id": "C", "amount_minor": 100}
    )
    snapshot = state(model)
    assert model.submit(purchase) == replace(denied, replayed=True)
    assert state(model) == snapshot
    assert model.submit({**purchase, "operation_id": "P2", "key": "P2-KEY"}).http_status == 201
    assert {key: row["booked"] for key, row in model.balances.items()} == {
        "CL": 200,
        "C": 99,
        "M1": 101,
        "M2": 0,
    }
    assert "P1" not in model.journals and "P2" in model.journals


def test_manual_manifest_catches_wrong_stored_request_as_extra_test_assurance(corpus):
    model = run_prefix(corpus, 1)
    expected_step = corpus["steps"][1]
    # Runtime intent B sees retained requests. This test-only oracle separately
    # catches a producer that accidentally stores a valid request targeting M2.
    wrong_saved_input = {**expected_step["command"], "merchant_account_id": "M2"}
    result = model.submit(wrong_saved_input)
    assert result.http_status == 201
    assert {key: row["booked"] for key, row in model.balances.items()} == {
        "CL": 100,
        "C": 20,
        "M1": 0,
        "M2": 80,
    }
    with pytest.raises(AssertionError):
        assert_step(model, result, expected_step["expected"])


def test_idle_account_remains_in_every_balance_snapshot(corpus):
    model = run_prefix(corpus, len(corpus["steps"]))
    assert model.balances["M2"] == {"debits": 0, "credits": 0, "booked": 0, "reserved": 0}
    snapshot = model.balances
    snapshot.pop("M2")
    assert snapshot != corpus["expected_final_totals"]
    assert "M2" in model.balances  # Returned snapshot is not mutable model state.


def test_input_mutation_after_admission_does_not_change_retained_intent(corpus):
    model = run_prefix(corpus, 1)
    purchase = deepcopy(corpus["steps"][1]["command"])
    model.submit(purchase)
    purchase["amount_minor"] = 999
    assert model.operations["P1"].command["amount_minor"] == 80
    original = corpus["steps"][1]["command"]
    assert model.submit(original).replayed is True


def test_arithmetic_uses_python_integers_and_rejects_boolean_amount(corpus):
    model = model_for(corpus)
    maximum = 9_000_000_000_000_000
    model.submit(
        {
            "kind": "OPENING",
            "operation_id": "LARGE",
            "credit_account_id": "C",
            "amount_minor": maximum,
        }
    )
    purchase = {**corpus["steps"][1]["command"], "amount_minor": maximum}
    assert model.submit(purchase).http_status == 201
    assert model.balances["C"]["booked"] == 0
    assert model.balances["M1"]["booked"] == maximum
    before = state(model)
    with pytest.raises(ModelError, match="^INVALID_REQUEST$"):
        model.submit({**purchase, "operation_id": "BOOL", "key": "BOOL", "amount_minor": True})
    assert state(model) == before


def test_command_oracle_imports_no_production_or_sql_helpers():
    source = CORPUS.parents[2] / "reference/command_model.py"
    imports = [
        node
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    names = [alias.name for node in imports if isinstance(node, ast.Import) for alias in node.names]
    names += [node.module for node in imports if isinstance(node, ast.ImportFrom)]
    assert names == ["copy", "dataclasses"]


@pytest.mark.parametrize("key", ["RA-KEY", "fresh-request-key"])
def test_collision_authorizes_retained_original_not_only_incoming_original(corpus, key):
    model = run_prefix(corpus, 5)
    model.submit(
        {
            "kind": "OPENING",
            "operation_id": "C-EXTRA",
            "credit_account_id": "C",
            "amount_minor": 100,
        }
    )
    model.submit(
        {
            **corpus["steps"][1]["command"],
            "operation_id": "P-M2",
            "key": "P-M2",
            "merchant_account_id": "M2",
            "amount_minor": 10,
        }
    )
    # Merchant2 owns the incoming original, but RA belongs to Merchant1. Neither
    # a request-key collision nor a business-ID collision may reveal a 409/result.
    incoming = {
        **corpus["steps"][4]["command"],
        "actor_id": "MERCHANT-2",
        "original_operation_id": "P-M2",
        "key": key,
    }
    before = state(model)
    with pytest.raises(ModelError, match="^NOT_FOUND$"):
        model.submit(incoming)
    assert state(model) == before
