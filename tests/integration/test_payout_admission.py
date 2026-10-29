"""Real PostgreSQL payout admission: hold, intent, identity and commit uncertainty."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import (
    BusinessIdConflict,
    IdempotencyConflict,
    NotAuthorized,
    RetrySameIdentity,
    submit_payout,
)
from ledgerops.db.maintenance import freeze_workspace
from ledgerops.domain import ActorId, BeneficiaryId, ProviderNamespaceId
from tests.integration.concurrency_helpers import finish_call, start_call, wait_for_blocker
from tests.integration.payout_helpers import (
    BENEFICIARY,
    NAMESPACE,
    NOW,
    PAYOUT_ID,
    admit,
    fresh_input,
    fund_merchant,
    money,
    payout_input,
    payout_policy,
    payout_state,
)
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.purchase_helpers import fund_customer
from tests.integration.reversal_helpers import reversal_input, reverse
from tests.integration.test_purchase_recovery import (
    _runtime_connection_settings,
    _wait_backend_gone,
)

pytestmark = pytest.mark.integration


def test_admission_reserves_gross_without_posting_and_binds_every_retained_byte(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    before = payout_state(seeded_db)
    args = payout_input(golden)
    result = admit(seeded_db, golden, args)
    assert (result.http_status, result.replayed) == (202, False)
    assert result.body == {
        "operation_id": PAYOUT_ID,
        "payout_id": PAYOUT_ID,
        "admission": "accepted_for_processing",
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "dispatch_deadline": "2026-10-29T12:01:00Z",
        "contract_version": 1,
    }
    assert money(seeded_db, golden) == (80, 62, 18, 2)
    after = payout_state(seeded_db)
    assert after["journal"] == before["journal"]
    assert after["journal_entry"] == before["journal_entry"]
    assert [
        len(after[t]) for t in ("payout", "reservation", "reservation_event", "dispatch_intent")
    ] == [1, 1, 1, 1]
    assert after["transport_attempt"] == []
    expected_request = {
        "workspace_id": golden["workspace"]["id"],
        "operation_id": PAYOUT_ID,
        "merchant_id": golden["purchase_request"]["merchant_account_id"],
        "beneficiary_id": BENEFICIARY,
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "currency": "PLN",
        "contract_version": 1,
        "dispatch_deadline": "2026-10-29T12:01:00Z",
    }
    expected_bytes = json.dumps(expected_request, sort_keys=True, separators=(",", ":")).encode()
    with seeded_db.connect("reader") as conn:
        assert conn.execute(
            "SELECT provider_key,provider_request,provider_request_sha256,knowledge "
            "FROM ledger.payout"
        ).fetchone() == (
            f"v1.{NAMESPACE}.{golden['workspace']['id']}.{PAYOUT_ID}",
            expected_bytes,
            hashlib.sha256(expected_bytes).digest(),
            "NOT_SENT",
        )
        assert conn.execute(
            "SELECT gross_minor,state,version FROM ledger.reservation"
        ).fetchone() == (62, "HELD", 1)
        assert conn.execute(
            "SELECT event_no,state,observation_id FROM ledger.reservation_event"
        ).fetchone() == (1, "HELD", None)
        assert conn.execute(
            "SELECT state,attempts,last_attempt_id FROM ledger.dispatch_intent"
        ).fetchone() == ("READY", 0, None)
        assert conn.execute(
            "SELECT admission,http_status,response_body,payload,payload_sha256 "
            "FROM ledger.operation WHERE kind='PAYOUT'"
        ).fetchone() == (
            "ACCEPTED",
            202,
            result.body,
            args[1].canonical_bytes,
            args[1].payload_sha256,
        )
        assert conn.execute(
            "SELECT http_status,response_body FROM ledger.request_result "
            "WHERE route_family='payout:v1'"
        ).fetchone() == (202, result.body)


@pytest.mark.parametrize("new_key", [False, True])
def test_replay_retains_deadline_and_receipt_when_clock_policy_and_enablement_change(
    seeded_db, golden, new_key
):
    fund_merchant(seeded_db, golden)
    args = payout_input(golden)
    original = admit(seeded_db, golden, args)
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "UPDATE ledger.account SET enabled=false WHERE id=%s",
            (str(args[1].merchant_account_id),),
        )
    before = payout_state(seeded_db)
    changed = payout_policy(
        golden,
        fixed_fee_minor=9,
        dispatch_ttl_seconds=900,
        beneficiary_owners={BeneficiaryId.parse(BENEFICIARY): ActorId.parse(str(uuid4()))},
    )
    principal, command, key = args
    replay = admit(
        seeded_db,
        golden,
        (principal, command, replace(key, key="copy") if new_key else key),
        policy=changed,
        now=NOW + timedelta(days=1),
    )
    assert (replay.http_status, replay.body, replay.replayed) == (202, original.body, True)
    after = payout_state(seeded_db)
    if new_key:
        assert len(after.pop("request_result")) == len(before.pop("request_result")) + 1
    assert after == before


@pytest.mark.parametrize("changed", ["amount", "beneficiary", "namespace", "operation"])
def test_same_request_key_changed_business_bytes_conflicts_without_mutation(
    seeded_db, golden, changed
):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    before = payout_state(seeded_db)
    change = {
        "amount": {"principal_minor": "61"},
        "beneficiary": {"beneficiary_id": str(uuid4())},
        "namespace": {"provider_namespace": str(uuid4())},
        "operation": {"operation_id": str(uuid4())},
    }[changed]
    with pytest.raises(IdempotencyConflict):
        admit(seeded_db, golden, payout_input(golden, **change))
    assert payout_state(seeded_db) == before


def test_new_key_same_business_different_bytes_conflicts_then_correct_payload_links(
    seeded_db, golden
):
    fund_merchant(seeded_db, golden)
    original = admit(seeded_db, golden)
    before = payout_state(seeded_db)
    with pytest.raises(BusinessIdConflict):
        admit(seeded_db, golden, payout_input(golden, key="second", principal_minor="61"))
    assert payout_state(seeded_db) == before
    assert admit(seeded_db, golden, payout_input(golden, key="second")).body == original.body
    assert money(seeded_db, golden) == (80, 62, 18, 2)


@pytest.mark.parametrize("unauthorized", ["actor", "beneficiary", "namespace", "fee", "disabled"])
def test_new_admission_requires_owner_beneficiary_policy_and_enabled_account(
    seeded_db, golden, unauthorized
):
    fund_merchant(seeded_db, golden)
    args, policy = payout_input(golden), payout_policy(golden)
    if unauthorized == "actor":
        args = (replace(args[0], actor_id=ActorId.parse(str(uuid4()))), args[1], args[2])
    elif unauthorized == "beneficiary":
        policy = payout_policy(
            golden,
            beneficiary_owners={BeneficiaryId.parse(BENEFICIARY): ActorId.parse(str(uuid4()))},
        )
    elif unauthorized == "namespace":
        policy = payout_policy(golden, provider_namespace=ProviderNamespaceId.parse(str(uuid4())))
    elif unauthorized == "fee":
        policy = payout_policy(golden, fixed_fee_minor=3)
    else:
        with seeded_db.connect("owner") as conn:
            conn.execute(
                "UPDATE ledger.account SET enabled=false WHERE id=%s",
                (str(args[1].merchant_account_id),),
            )
    before = payout_state(seeded_db)
    with pytest.raises(NotAuthorized) as caught:
        admit(seeded_db, golden, args, policy=policy)
    assert caught.value.http_status == 404
    assert payout_state(seeded_db) == before


def test_insufficient_result_is_retained_and_new_business_after_funding_can_succeed(
    seeded_db, golden
):
    fund_merchant(seeded_db, golden)
    args = payout_input(golden, principal_minor="79")
    result = admit(seeded_db, golden, args)
    assert result.http_status == 422 and result.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert money(seeded_db, golden) == (80, 0, 80, 1)
    assert all(
        not payout_state(seeded_db)[table]
        for table in ("payout", "reservation", "reservation_event", "dispatch_intent")
    )
    fund_customer(
        seeded_db, golden, 10, customer_account_id=golden["purchase_request"]["merchant_account_id"]
    )
    assert admit(seeded_db, golden, args).body == result.body
    accepted = admit(seeded_db, golden, fresh_input(golden, principal_minor="79"))
    assert accepted.http_status == 202
    assert money(seeded_db, golden) == (90, 81, 9, 3)


def test_held_money_cannot_be_spent_by_purchase_reversal(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    admit(seeded_db, golden)
    result = reverse(seeded_db, *reversal_input(golden))
    assert result.http_status == 422 and result.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert money(seeded_db, golden) == (80, 62, 18, 2)


def test_frozen_workspace_refuses_new_hold_before_identity_claim(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, golden["workspace"]["id"], str(uuid4()), str(uuid4()), "inspect")
    before = payout_state(seeded_db)
    with pytest.raises(RetrySameIdentity):
        admit(seeded_db, golden)
    assert payout_state(seeded_db) == before


@pytest.mark.parametrize(
    "stage", ["payout", "reservation", "reservation_event", "dispatch_intent", "balance", "commit"]
)
def test_admission_sql_or_commit_failure_rolls_back_hold_intent_and_identity(
    seeded_db, golden, stage
):
    fund_merchant(seeded_db, golden)
    before = payout_state(seeded_db)
    table = {"balance": "account_balance", "commit": "request_result"}.get(stage, stage)
    event = "UPDATE" if stage in {"balance", "commit"} else "INSERT"
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("""CREATE FUNCTION ledger.test_payout_failure() RETURNS trigger
            LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION USING ERRCODE='23514',
            CONSTRAINT='test_payout_failure',MESSAGE='injected payout failure'; END $$""")
        conn.execute(
            sql.SQL(
                "CREATE {constraint}TRIGGER test_payout_failure AFTER {event} ON ledger.{table} "
                "{deferred}FOR EACH ROW EXECUTE FUNCTION ledger.test_payout_failure()"
            ).format(
                constraint=sql.SQL("CONSTRAINT " if stage == "commit" else ""),
                event=sql.SQL(event),
                table=sql.Identifier(table),
                deferred=sql.SQL("DEFERRABLE INITIALLY DEFERRED " if stage == "commit" else ""),
            )
        )
    with seeded_db.connect("app") as conn, pytest.raises(errors.CheckViolation) as caught:
        submit_payout(conn, *payout_input(golden), policy=payout_policy(golden), clock=lambda: NOW)
    assert caught.value.diag.constraint_name == "test_payout_failure"
    assert payout_state(seeded_db) == before
    with seeded_db.connect("owner") as conn:
        conn.execute(
            sql.SQL("DROP TRIGGER test_payout_failure ON ledger.{}").format(sql.Identifier(table))
        )
    assert admit(seeded_db, golden).http_status == 202
    assert money(seeded_db, golden) == (80, 62, 18, 2)


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_unknown_commit_recovered_using_same_identity_has_exactly_one_hold(
    seeded_db, golden, fault
):
    fund_merchant(seeded_db, golden)
    args = payout_input(golden)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        with psycopg.connect(**_runtime_connection_settings(seeded_db, proxy.port)) as conn:
            pid = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                submit_payout(conn, *args, policy=payout_policy(golden), clock=lambda: NOW)
        assert (
            proxy.finished.wait(5)
            and proxy.frontend_commit_seen.is_set()
            and proxy.fault_applied.is_set()
        )
        assert proxy.backend_commit_seen.is_set() == (fault == "after-commit")
    _wait_backend_gone(seeded_db, pid)
    recovered = admit(seeded_db, golden, args)
    assert recovered.http_status == 202 and recovered.replayed == (fault == "after-commit")
    assert money(seeded_db, golden) == (80, 62, 18, 2)
    state = payout_state(seeded_db)
    assert (
        len(state["reservation"])
        == len(state["reservation_event"])
        == len(state["dispatch_intent"])
        == 1
    )


def test_two_real_concurrent_payouts_cannot_reserve_the_same_money(seeded_db, golden):
    fund_merchant(seeded_db, golden)
    args = [payout_input(golden), fresh_input(golden)]
    workers = []
    with seeded_db.connect("owner") as blocker, seeded_db.connect("admin") as observer:
        with seeded_db.connect("app") as first, seeded_db.connect("app") as second:
            try:
                with blocker.transaction():
                    blocker.execute(
                        "SELECT 1 FROM ledger.account_balance WHERE account_id=%s FOR UPDATE",
                        (str(args[0][1].merchant_account_id),),
                    )
                    for conn, arguments in zip((first, second), args, strict=True):
                        worker = start_call(
                            lambda c=conn, a=arguments: submit_payout(
                                c, *a, policy=payout_policy(golden), clock=lambda: NOW
                            )
                        )
                        workers.append(worker)
                        wait_for_blocker(
                            observer,
                            conn.info.backend_pid,
                            (blocker.info.backend_pid, first.info.backend_pid),
                            worker,
                            query_fragment="account_balance",
                        )
                results = [finish_call(worker) for worker in workers]
                assert all(kind == "returned" for kind, _ in results), results
                assert sorted(result.http_status for _, result in results) == [202, 422]
                assert money(seeded_db, golden) == (80, 62, 18, 2)
            finally:
                for conn, worker in zip((first, second), workers, strict=False):
                    if worker.thread.is_alive():
                        conn.cancel()
                        worker.thread.join(5)


def test_requires_idle_autocommit_connection(seeded_db, golden):
    with seeded_db.connect("app") as conn, conn.transaction():
        with pytest.raises(ValueError, match="idle autocommit"):
            submit_payout(
                conn, *payout_input(golden), policy=payout_policy(golden), clock=lambda: NOW
            )
        assert conn.info.transaction_status == TransactionStatus.INTRANS


def test_year_one_deadline_is_canonical_and_does_not_depend_on_strftime_padding(seeded_db, golden):
    from datetime import UTC, datetime

    fund_merchant(seeded_db, golden)
    result = admit(seeded_db, golden, now=datetime(1, 1, 1, tzinfo=UTC))
    assert result.body["dispatch_deadline"] == "0001-01-01T00:01:00Z"
    assert money(seeded_db, golden)[:3] == (80, 62, 18)


def test_clock_deadline_overflow_rolls_back_claims_without_a_partial_hold(seeded_db, golden):
    from datetime import UTC, datetime

    fund_merchant(seeded_db, golden)
    before = payout_state(seeded_db)
    with pytest.raises(OverflowError):
        admit(seeded_db, golden, now=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC))
    assert payout_state(seeded_db) == before


def test_progress_query_and_replay_recheck_stored_merchant_ownership(seeded_db, golden):
    from ledgerops.db.admission import get_payout_state

    fund_merchant(seeded_db, golden)
    args = payout_input(golden)
    admit(seeded_db, golden, args)
    with seeded_db.connect("app") as conn:
        status = get_payout_state(conn, args[0], args[1].operation_id)
        assert status["knowledge"] == "NOT_SENT"
        assert status["reservation_state"] == "HELD" and status["dispatch_state"] == "READY"
        with pytest.raises(NotAuthorized):
            get_payout_state(
                conn,
                replace(args[0], actor_id=ActorId.parse(golden["accounts"][3]["owner_id"])),
                args[1].operation_id,
            )
    before = payout_state(seeded_db)
    other = payout_input(
        golden,
        actor_id=golden["accounts"][3]["owner_id"],
        merchant_account_id=golden["accounts"][3]["id"],
    )
    with pytest.raises(NotAuthorized):
        admit(seeded_db, golden, other)
    assert payout_state(seeded_db) == before
