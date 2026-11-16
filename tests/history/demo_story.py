"""Run the same money movement with and without its balance lock."""

import json

import pytest

from tests.history import test_lock_mutant as cases

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("locked", [True, False], ids=["locked-control", "unlocked-variant"])
def test_history_story(seeded_db, golden, history_recorder, tmp_path, locked):
    label = "LOCKED CONTROL" if locked else "UNLOCKED VARIANT"
    print(f"\n{label}: two concurrent purchases of 30 from 100", flush=True)
    if locked:
        cases.test_locked_control_serializes_the_same_two_purchases(
            seeded_db, golden, history_recorder
        )
    else:
        cases.test_unlocked_purchases_keep_correct_totals_but_history_shows_lost_update(
            seeded_db, golden, history_recorder, tmp_path
        )
    balances = cases._balances(seeded_db)
    customer = golden["purchase_request"]["customer_account_id"]
    merchant = golden["purchase_request"]["merchant_account_id"]
    assert (balances[customer], balances[merchant]) == (40, 60)
    print("Both purchases returned 201. Customer=40; merchant=60; independent fold=MATCH.")
    report, _ = cases._history_report(seeded_db, history_recorder, tmp_path)
    assert report["valid"] is locked
    verdict = "PASS" if report["valid"] else "REJECTED: " + ", ".join(report["anomalies"])
    print("Offline history verdict: " + verdict, flush=True)
    print("Checker report: " + json.dumps(report, sort_keys=True), flush=True)
    if not locked:
        print(
            "Correct totals hid a stale read. The history checker rejected the execution.",
            flush=True,
        )
