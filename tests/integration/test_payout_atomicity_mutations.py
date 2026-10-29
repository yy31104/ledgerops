"""Two transaction-splitting defects must fail the real financial success contract.

The mutations issue valid COMMIT statements after real financial SQL. Only an
identified deferred financial guard counts; arbitrary exceptions or a hung test do not.
"""

import psycopg
import pytest
from psycopg import errors

from ledgerops.db.admission import submit_payout
from ledgerops.db.payout_resolution import receive_observation
from tests.integration.payout_helpers import (
    NOW,
    admit,
    fund_merchant,
    money,
    payout_input,
    payout_policy,
    payout_state,
)
from tests.integration.payout_observation_helpers import (
    event,
    observe,
    prepared_payout,
    resolution_state,
    trust,
)
from tests.integration.test_payout_resolution import check_final

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("split_transaction", [False, True], ids=["atomic", "split-hold-intent"])
def test_admission_success_contract_detects_committing_hold_before_intent(
    seeded_db, golden, split_transaction
):
    fund_merchant(seeded_db, golden)
    args = payout_input(golden)
    before = payout_state(seeded_db)
    injected = []

    class CommitAfterHold(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            result = super().execute(query, params, **kwargs)
            if isinstance(query, str) and query.startswith(
                "UPDATE ledger.account_balance SET reserved=reserved+"
            ):
                injected.append(query)
                # A hypothetical first transaction commits the hold, planning
                # to insert dispatch_intent in a second transaction afterward.
                super().execute("COMMIT")
            return result

    with seeded_db.connect("app") as conn:
        if split_transaction:
            conn.cursor_factory = CommitAfterHold

        def admission_must_commit_one_complete_202():
            try:
                result = submit_payout(conn, *args, policy=payout_policy(golden), clock=lambda: NOW)
            except errors.CheckViolation as exc:
                # The original request is still pending at this split point.
                # Its deferred guard is part of the same atomicity contract.
                assert exc.diag.message_primary == "request result is incomplete"
                assert "assert_request_terminal" in exc.diag.context
                raise AssertionError("admission must commit one complete 202") from exc
            assert result.http_status == 202
            assert money(seeded_db, golden)[:3] == (80, 62, 18)

        if split_transaction:
            with pytest.raises(AssertionError, match="admission must commit one complete 202"):
                admission_must_commit_one_complete_202()
            assert len(injected) == 1
            assert payout_state(seeded_db) == before
        else:
            admission_must_commit_one_complete_202()
            assert injected == []

    recovered = admit(seeded_db, golden, args)
    assert recovered.http_status == 202
    assert recovered.replayed is (not split_transaction)
    assert money(seeded_db, golden)[:3] == (80, 62, 18)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT count(*) FROM ledger.reservation").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM ledger.dispatch_intent").fetchone() == (1,)


@pytest.mark.parametrize(
    "split_transaction", [False, True], ids=["atomic", "split-journal-consume"]
)
def test_terminal_success_contract_detects_committing_journal_before_hold_consumption(
    seeded_db, golden, split_transaction
):
    command = prepared_payout(seeded_db, golden)
    raw = event(seeded_db, command)
    before = resolution_state(seeded_db)
    injected = []

    class CommitAfterJournal(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            result = super().execute(query, params, **kwargs)
            if isinstance(query, str) and query.startswith("INSERT INTO ledger.journal_entry "):
                injected.append(query)
                # Both sides of the journal have been inserted: merely checking
                # balanced entries cannot establish complete hold consumption.
                super().execute("COMMIT")
            return result

    with seeded_db.connect("worker") as conn:
        if split_transaction:
            conn.cursor_factory = CommitAfterJournal

        def terminal_must_commit_one_complete_effect():
            try:
                result = receive_observation(
                    conn,
                    command.workspace_id,
                    command.operation_id,
                    raw,
                    trust=trust(command),
                    clock=lambda: NOW,
                )
            except errors.CheckViolation as exc:
                assert exc.diag.message_primary == (
                    "unresolved payout cannot have a terminal financial effect"
                )
                assert "assert_payout_admission" in exc.diag.context
                raise AssertionError(
                    "success must commit journal and consumed hold together"
                ) from exc
            assert result.status == "APPLIED"
            check_final(seeded_db, golden, command, "SUCCEEDED")

        if split_transaction:
            with pytest.raises(
                AssertionError, match="success must commit journal and consumed hold together"
            ):
                terminal_must_commit_one_complete_effect()
            assert len(injected) == 1
            assert resolution_state(seeded_db) == before
        else:
            terminal_must_commit_one_complete_effect()
            assert injected == []

    assert observe(seeded_db, command, raw).status == (
        "APPLIED" if split_transaction else "REPLAYED"
    )
    check_final(seeded_db, golden, command, "SUCCEEDED")
