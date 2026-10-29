"""Terminal lifecycle and immutable evidence boundaries using actual database roles."""

import pytest
from psycopg import errors

from tests.integration.payout_observation_helpers import (
    event,
    observe,
    prepared_payout,
    raw_table,
    resolution_state,
)

pytestmark = pytest.mark.integration


def test_hold_cannot_be_released_without_matching_authoritative_resolution(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    before = resolution_state(seeded_db)
    with (
        seeded_db.connect("worker") as conn,
        pytest.raises(errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(
            "UPDATE ledger.reservation SET state='RELEASED',version=2 WHERE payout_id=%s",
            (str(command.operation_id),),
        )
        conn.execute(
            "UPDATE ledger.account_balance SET reserved=0,version=version+1 WHERE account_id=%s",
            (str(command.merchant_account_id),),
        )
    assert resolution_state(seeded_db) == before


@pytest.mark.parametrize(
    "table,field",
    [
        ("provider_observation", "signed_payload"),
        ("observation_receipt", "channel"),
        ("observation_quarantine", "reason"),
        ("payout_incident", "code"),
        ("payout_resolution", "state"),
    ],
)
def test_saved_evidence_cannot_be_edited_or_deleted_even_by_accidental_owner_sql(
    seeded_db, golden, table, field
):
    command = prepared_payout(seeded_db, golden)
    observe(seeded_db, command, b'{"invalid":true}')
    observe(seeded_db, command, event(seeded_db, command, sequence=10))
    observe(seeded_db, command, event(seeded_db, command, "FAILED", sequence=2))
    assert raw_table(seeded_db, table)
    before = resolution_state(seeded_db)
    with seeded_db.connect("owner") as conn:
        for statement in (
            f"UPDATE ledger.{table} SET {field}={field}",
            f"DELETE FROM ledger.{table}",
        ):
            with pytest.raises(errors.InsufficientPrivilege, match="immutable"):
                conn.execute(statement)
    assert resolution_state(seeded_db) == before


@pytest.mark.parametrize(
    "table",
    [
        "provider_observation",
        "observation_receipt",
        "observation_quarantine",
        "payout_incident",
        "payout_resolution",
    ],
)
def test_admission_role_cannot_insert_provider_authority_or_resolution(seeded_db, golden, table):
    prepared_payout(seeded_db, golden)
    with seeded_db.connect("app") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(f"INSERT INTO ledger.{table} SELECT * FROM ledger.{table}")
