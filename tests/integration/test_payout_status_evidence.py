"""Authorized, bounded evidence references from one actual PostgreSQL snapshot."""

import json
from datetime import timedelta

import pytest

from ledgerops.db import payout_resolution
from ledgerops.db.admission import NotAuthorized, get_payout_state
from tests.integration.payout_helpers import NOW, payout_input
from tests.integration.payout_observation_helpers import (
    event,
    lookup,
    observe,
    prepared_payout,
)

pytestmark = pytest.mark.integration


def read_state(db, golden, command):
    with db.connect("reader") as conn:
        return get_payout_state(conn, payout_input(golden)[0], command.operation_id)


def test_status_bounds_recent_references_and_keeps_complete_terminal_reference(seeded_db, golden):
    command = prepared_payout(seeded_db, golden)
    assert read_state(seeded_db, golden, command)["recent_observations"] == []
    assert read_state(seeded_db, golden, command)["resolution"] is None
    references = []
    for i in range(7):
        result = observe(
            seeded_db, command, lookup(seeded_db, command), now=NOW + timedelta(seconds=i)
        )
        references.append(str(result.observation_id))
    current = read_state(seeded_db, golden, command)
    assert [row["observation_id"] for row in current["recent_observations"]] == references[-5:][
        ::-1
    ]
    for row in current["recent_observations"]:
        assert row.keys() == {"observation_id", "kind", "state", "source_sequence"}
        assert (row["kind"], row["state"], row["source_sequence"]) == ("lookup", "NOT_FOUND", 0)
    result = observe(seeded_db, command, event(seeded_db, command), now=NOW + timedelta(seconds=8))
    current = read_state(seeded_db, golden, command)
    with seeded_db.connect("reader") as conn:
        journal_id = conn.execute("SELECT journal_id FROM ledger.payout_resolution").fetchone()[0]
    assert current["resolution"] == {
        "state": "SUCCEEDED",
        "observation_id": str(result.observation_id),
        "journal_id": str(journal_id),
    }
    assert current["recent_observations"][0]["observation_id"] == str(result.observation_id)
    assert len(current["recent_observations"]) == 5
    # The status service returns references and fixed classifications, not source
    # payloads, HMAC signatures, provider keys, or authentication configuration.
    serialized = json.dumps(current)
    for forbidden in ("signed_payload", "signature", "provider_key", "beneficiary_id", "test-v1"):
        assert forbidden not in serialized
    with seeded_db.connect("reader") as conn, pytest.raises(NotAuthorized):
        get_payout_state(
            conn,
            payout_input(golden, actor_id=golden["purchase_request"]["customer_id"])[0],
            command.operation_id,
        )


@pytest.mark.parametrize("terminal", ["FAILED", "CLOSED_ABSENT"])
def test_negative_terminal_reference_explicitly_has_no_journal(seeded_db, golden, terminal):
    command = prepared_payout(seeded_db, golden)
    result = observe(seeded_db, command, event(seeded_db, command, terminal))
    current = read_state(seeded_db, golden, command)
    assert current["resolution"] == {
        "state": terminal,
        "observation_id": str(result.observation_id),
        "journal_id": None,
    }


def test_terminal_commit_between_status_and_evidence_reads_cannot_mix_two_snapshots(
    seeded_db, golden, monkeypatch
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    original = payout_resolution.blocking_incidents
    committed = []

    def commit_after_first_status_read(cur, workspace, payout_id):
        if not committed:
            committed.append(True)
            assert observe(seeded_db, command, raw).status == "APPLIED"
        return original(cur, workspace, payout_id)

    # The hook only arranges a real second-connection COMMIT between the reads;
    # both status and evidence still come from genuine PostgreSQL queries.
    monkeypatch.setattr(payout_resolution, "blocking_incidents", commit_after_first_status_read)
    old = read_state(seeded_db, golden, command)
    assert old["knowledge"] == "NOT_SENT" and old["reservation_state"] == "HELD"
    assert old["recent_observations"] == [] and old["resolution"] is None
    new = read_state(seeded_db, golden, command)
    assert new["knowledge"] == "SUCCEEDED" and new["reservation_state"] == "CONSUMED"
    assert new["resolution"]["observation_id"] == new["recent_observations"][0]["observation_id"]
