"""Real PostgreSQL errors, whole-transaction retry and wire-level commit ambiguity."""

from dataclasses import replace

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from ledgerops.db import admission
from ledgerops.db.admission import RetrySameIdentity, submit_reversal
from tests.integration.postgres_fault_proxy import PostgresCommitProxy
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import (
    assert_balances,
    reversal_input,
    reversal_state,
    reverse,
)
from tests.integration.test_purchase_recovery import (
    _runtime_connection_settings,
    _wait_backend_gone,
)

pytestmark = pytest.mark.integration
FAULTS = {
    "attempt": ("reversal_attempt", "INSERT", False),
    "entry": ("journal_entry", "INSERT", False),
    "projection": ("account_balance", "UPDATE", False),
    "effect": ("purchase_reversal_effect", "INSERT", False),
    "unexpected-unique": ("purchase_reversal_effect", "INSERT", False),
    "operation": ("operation", "UPDATE", False),
    "request": ("request_result", "UPDATE", False),
    "commit": ("operation", "UPDATE", True),
}


def _guard(db, stage, install):
    table, event, deferred = FAULTS[stage]
    with db.connect("owner") as conn, conn.transaction():
        if not install:
            conn.execute(
                sql.SQL("DROP TRIGGER test_reversal_atomicity ON ledger.{}").format(
                    sql.Identifier(table)
                )
            )
            conn.execute("DROP FUNCTION ledger.test_reversal_atomicity()")
            return
        conn.execute("""
            CREATE FUNCTION ledger.test_reversal_atomicity() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
            BEGIN
              IF TG_ARGV[0]='commit' THEN
                IF NEW.kind <> 'REVERSAL' OR NEW.admission <> 'ACCEPTED'
                  OR NOT EXISTS (SELECT 1 FROM ledger.purchase_reversal_effect
                    WHERE workspace_id=NEW.workspace_id AND reversal_operation_id=NEW.operation_id)
                  OR NOT EXISTS (SELECT 1 FROM ledger.request_result
                    WHERE workspace_id=NEW.workspace_id AND operation_id=NEW.operation_id
                      AND http_status=201) THEN
                  RAISE EXCEPTION 'commit fault was not reached after complete reversal';
                END IF;
              END IF;
              RAISE EXCEPTION USING ERRCODE=TG_ARGV[1],
                CONSTRAINT='test_reversal_atomicity', MESSAGE='injected reversal ' || TG_ARGV[0];
            END $$
        """)
        conn.execute(
            sql.SQL(
                "CREATE {constraint}TRIGGER test_reversal_atomicity "
                "AFTER {event} ON ledger.{table} {deferred}FOR EACH ROW "
                "EXECUTE FUNCTION ledger.test_reversal_atomicity({stage},{state})"
            ).format(
                constraint=sql.SQL("CONSTRAINT " if deferred else ""),
                event=sql.SQL(event),
                table=sql.Identifier(table),
                deferred=sql.SQL("DEFERRABLE INITIALLY DEFERRED " if deferred else ""),
                stage=sql.Literal(stage),
                state=sql.Literal("23505" if stage == "unexpected-unique" else "23514"),
            )
        )


@pytest.mark.parametrize("stage", FAULTS)
def test_every_reversal_effect_rolls_back_after_sql_failure_and_same_id_recovers(
    seeded_db, golden, stage
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    arguments = reversal_input(golden)
    before = reversal_state(seeded_db)
    _guard(seeded_db, stage, True)
    try:
        with seeded_db.connect("app") as conn:
            expected = (
                errors.UniqueViolation if stage == "unexpected-unique" else errors.CheckViolation
            )
            with pytest.raises(expected) as caught:
                submit_reversal(conn, *arguments)
            assert caught.value.diag.constraint_name == "test_reversal_atomicity"
            assert caught.value.diag.message_primary == "injected reversal " + stage
            assert conn.info.transaction_status == TransactionStatus.IDLE
        assert reversal_state(seeded_db) == before
    finally:
        _guard(seeded_db, stage, False)
    result = reverse(seeded_db, *arguments)
    assert result.http_status == 201 and not result.replayed
    assert_balances(seeded_db, golden, [100, 100, 0, 0])
    committed = reversal_state(seeded_db)
    replay = reverse(seeded_db, *arguments)
    assert replay.replayed and replay.body == result.body
    assert reversal_state(seeded_db) == committed


@pytest.mark.parametrize("fault", ["before-forward", "after-commit"])
def test_reversal_unknown_commit_recovers_original_identity_without_duplicate_effect(
    seeded_db, golden, fault
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    arguments = reversal_input(golden)
    before = reversal_state(seeded_db)
    with PostgresCommitProxy(seeded_db.port, fault=fault) as proxy:
        with psycopg.connect(**_runtime_connection_settings(seeded_db, proxy.port)) as conn:
            backend = conn.info.backend_pid
            with pytest.raises(psycopg.OperationalError):
                submit_reversal(conn, *arguments)
            assert conn.closed
        assert proxy.finished.wait(5) and proxy.frontend_commit_seen.is_set()
        assert proxy.fault_applied.is_set() and proxy.commit_completions_forwarded == 0
        assert proxy.commit_queries_forwarded == (0 if fault == "before-forward" else 1)
        assert proxy.backend_commit_seen.is_set() is (fault == "after-commit")
    _wait_backend_gone(seeded_db, backend)
    if fault == "before-forward":
        assert reversal_state(seeded_db) == before
    recovered = reverse(seeded_db, *arguments)
    assert recovered.http_status == 201 and recovered.replayed is (fault == "after-commit")
    assert_balances(seeded_db, golden, [100, 100, 0, 0])
    after = reversal_state(seeded_db)
    replay = reverse(seeded_db, *arguments)
    assert replay.replayed and replay.body == recovered.body
    assert reversal_state(seeded_db) == after
    principal, command, key = arguments
    new_key = reverse(seeded_db, principal, command, replace(key, key="recover-new-key"))
    assert new_key.replayed and new_key.body == recovered.body
    final = reversal_state(seeded_db)
    assert len(final["request_result"]) == len(after["request_result"]) + 1
    for table in after.keys() - {"request_result"}:
        assert final[table] == after[table]


@pytest.mark.parametrize("failures,state", [(2, "40001"), (3, "40001"), (1, "23514")])
def test_reversal_uses_bounded_whole_transaction_retry_only_for_definite_aborts(
    seeded_db, golden, monkeypatch, failures, state
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    arguments = reversal_input(golden)
    before = reversal_state(seeded_db)
    with seeded_db.connect("owner") as conn, conn.transaction():
        conn.execute("CREATE SEQUENCE ledger.test_reversal_attempt_number")
        conn.execute(
            "GRANT USAGE,SELECT ON SEQUENCE ledger.test_reversal_attempt_number TO ledger_app"
        )
        conn.execute("""
            CREATE FUNCTION ledger.test_reversal_retry() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
            DECLARE attempt bigint;
            BEGIN
              attempt := nextval('ledger.test_reversal_attempt_number');
              IF attempt <= TG_ARGV[0]::integer THEN
                RAISE EXCEPTION USING ERRCODE=TG_ARGV[1],
                  CONSTRAINT='test_reversal_retry', MESSAGE='injected reversal retry abort';
              END IF;
              RETURN NEW;
            END $$
        """)
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER test_reversal_retry AFTER UPDATE ON ledger.operation "
                "FOR EACH ROW WHEN (NEW.kind='REVERSAL' AND NEW.admission='ACCEPTED') "
                "EXECUTE FUNCTION ledger.test_reversal_retry({failures},{state})"
            ).format(failures=sql.Literal(str(failures)), state=sql.Literal(state))
        )
    pauses = []
    with seeded_db.connect("app") as conn:

        def backoff(delay):
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert reversal_state(seeded_db) == before
            pauses.append(delay)

        monkeypatch.setattr(admission, "sleep", backoff)
        if state == "23514":
            with pytest.raises(errors.CheckViolation):
                submit_reversal(conn, *arguments)
        elif failures == 3:
            with pytest.raises(RetrySameIdentity) as caught:
                submit_reversal(conn, *arguments)
            assert caught.value.reason == "transaction_retries_exhausted"
        else:
            assert submit_reversal(conn, *arguments).http_status == 201
            assert_balances(seeded_db, golden, [100, 100, 0, 0])
    assert len(pauses) == (0 if state == "23514" else 2)
    for attempt, delay in enumerate(pauses, 1):
        assert 0.01 * attempt <= delay <= 0.05 * attempt
    if failures == 3 or state == "23514":
        assert reversal_state(seeded_db) == before
    with seeded_db.connect("owner") as conn:
        assert conn.execute(
            "SELECT last_value FROM ledger.test_reversal_attempt_number"
        ).fetchone() == (1 if state == "23514" else 3,)
