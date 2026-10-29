"""Named semantic defects are rejected by literal business assertions on real stores."""

import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from ledgerops.db import payout_dispatch, payout_resolution
from ledgerops.db.maintenance import freeze_workspace
from ledgerops.export import capture_snapshot
from ledgerops.provider_client import ProviderHTTPClient
from ledgerops.verification import verify_snapshot
from tests.integration.payout_helpers import NOW, money
from tests.integration.payout_observation_helpers import (
    event,
    lookup,
    observe,
    prepared_payout,
    raw_table,
    resolution_state,
    trust,
)
from tests.integration.purchase_helpers import fund_customer
from tests.integration.test_payout_dispatch import Client, next_due, run, signed_lookup
from tests.provider.test_process import TOKEN, ProcessProvider, effect_rows

pytestmark = pytest.mark.integration


def assert_held(db, golden):
    assert tuple(money(db, golden)[:3]) == (80, 62, 18), "uncertainty must keep all62 reserved"


@pytest.mark.parametrize("failure", ["NOT_FOUND", "TIMEOUT"])
def test_releasing_reserved_on_uncertainty_is_detected(seeded_db, golden, monkeypatch, failure):
    command = prepared_payout(seeded_db, golden)
    original_finish = payout_dispatch.finish_claim
    original_observe = payout_resolution._observe_nonterminal

    def premature_release(cur):
        # A realistic faulty projection write: no fake signed final result is needed.
        cur.execute(
            "UPDATE ledger.account_balance SET reserved=0,version=version+1 "
            "WHERE workspace_id=%s AND account_id=%s",
            (str(command.workspace_id), str(command.merchant_account_id)),
        )

    if failure == "NOT_FOUND":

        def bad_nonterminal(cur, *args):
            original_observe(cur, *args)
            premature_release(cur)

        monkeypatch.setattr(payout_resolution, "_observe_nonterminal", bad_nonterminal)
        observe(seeded_db, command, lookup(seeded_db, command))
    else:

        def bad_finish(conn, *args, **kwargs):
            result = original_finish(conn, *args, **kwargs)
            with conn.transaction(), conn.cursor() as cur:
                premature_release(cur)
            return result

        monkeypatch.setattr(payout_dispatch, "finish_claim", bad_finish)

        def timeout(*_):
            raise TimeoutError("injected transport uncertainty")

        with seeded_db.connect("worker") as conn:
            result = run(conn, command, Client(conn, timeout), NOW)
            assert result.outcome == "TIMEOUT"
    with pytest.raises(AssertionError, match="uncertainty must keep"):
        assert_held(seeded_db, golden)
    assert tuple(money(seeded_db, golden)[:3]) == (80, 0, 80)
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, str(command.workspace_id)))
    assert not report.projection_ok
    assert len(raw_table(seeded_db, "reservation_event")) == 1
    assert raw_table(seeded_db, "payout_resolution") == []


def test_retry_with_new_identity_can_double_external_effect_and_oracle_detects_it(
    seeded_db, golden, monkeypatch, tmp_path
):
    command = prepared_payout(seeded_db, golden)
    provider = ProcessProvider(tmp_path, namespace=str(command.provider_namespace)).start()
    try:
        client = ProviderHTTPClient(provider.ready["url"], TOKEN)

        # The first effect really commits in the independent process, but its reply is lost.
        def lose_reply(action, key, raw):
            assert action == "PUT"
            assert client.put(key, raw).http_status == 200
            raise TimeoutError("response lost after provider commit")

        with seeded_db.connect("worker") as conn:
            assert run(conn, command, Client(conn, lose_reply), NOW).outcome == "TIMEOUT"
        first_effect = effect_rows(provider.path)
        assert len(first_effect) == 1 and first_effect[0][1:] == ("60", "2", "62")
        assert_held(seeded_db, golden)
        # A delayed visibility response causes the specified same-identity retry.
        with seeded_db.connect("worker") as conn:
            delayed = Client(conn, lambda _action, key, _raw: signed_lookup(key))
            assert run(conn, command, delayed, next_due(seeded_db)).observation.state == "NOT_FOUND"
        original = payout_dispatch._transport

        def changed_identity(adapter, claim):
            assert claim.action == "PUT"
            request = json.loads(claim.request_bytes)
            request["operation_id"] = str(uuid4())
            key = ".".join(
                (
                    "v1",
                    str(command.provider_namespace),
                    str(command.workspace_id),
                    request["operation_id"],
                )
            )
            return adapter.put(
                key, json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            )

        with monkeypatch.context() as patch:
            patch.setattr(payout_dispatch, "_transport", changed_identity)
            with seeded_db.connect("worker") as conn:
                result = run(conn, command, client, next_due(seeded_db))
            assert result.resolution.status == "QUARANTINED"
        assert payout_dispatch._transport is original
        with pytest.raises(
            AssertionError, match="one business request must have one provider effect"
        ):
            assert len(effect_rows(provider.path)) == 1, (
                "one business request must have one provider effect"
            )
        assert len(effect_rows(provider.path)) == 2
        assert_held(seeded_db, golden)
    finally:
        provider.stop()


def test_ignoring_old_contradiction_loses_incident_even_when_no_second_debit_occurs(
    seeded_db, golden, monkeypatch
):
    command = prepared_payout(seeded_db, golden)
    assert observe(seeded_db, command, event(seeded_db, command, sequence=10)).status == "APPLIED"
    with monkeypatch.context() as patch:
        original = payout_resolution._contradiction
        patch.setattr(
            payout_resolution,
            "_contradiction",
            lambda cur, row: (None, None) if row["source_sequence"] < 10 else original(cur, row),
        )
        mutant = observe(seeded_db, command, event(seeded_db, command, "FAILED", sequence=2))
    with pytest.raises(AssertionError, match="old opposite final evidence must stop automation"):
        assert mutant.status == "INCIDENT", "old opposite final evidence must stop automation"
    assert mutant.status == "REPLAYED"
    assert raw_table(seeded_db, "payout_incident") == []
    with seeded_db.connect("reader") as conn:
        assert not verify_snapshot(capture_snapshot(conn, str(command.workspace_id))).healthy


def test_deduplicating_event_id_without_bytes_hides_conflict_and_is_detected(
    seeded_db, golden, monkeypatch
):
    command = prepared_payout(seeded_db, golden)
    identifier = uuid4()
    assert (
        observe(seeded_db, command, event(seeded_db, command, event_id=identifier)).status
        == "APPLIED"
    )
    original = payout_resolution._retain

    def id_only(cur, payout, observation, now):
        row = cur.execute(
            "SELECT * FROM ledger.provider_observation WHERE provider_namespace=%s "
            "AND event_id=%s LIMIT 1",
            (payout["provider_namespace"], observation.event_id),
        ).fetchone()
        return (row, False) if row else original(cur, payout, observation, now)

    with monkeypatch.context() as patch:
        patch.setattr(payout_resolution, "_retain", id_only)
        mutant = observe(
            seeded_db, command, event(seeded_db, command, "FAILED", event_id=identifier, sequence=2)
        )
    with pytest.raises(AssertionError, match="changed signed bytes must retain both sides"):
        assert mutant.status == "INCIDENT", "changed signed bytes must retain both sides"
    assert len(raw_table(seeded_db, "provider_observation")) == 1
    assert raw_table(seeded_db, "payout_incident") == []


def test_omitting_reserved_decrement_can_pass_sql_checks_but_independent_fold_rejects_it(
    seeded_db, golden
):
    command = prepared_payout(seeded_db, golden)
    fund_customer(seeded_db, golden, 80, customer_account_id=str(command.merchant_account_id))
    altered = []

    class MutantCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            if isinstance(query, str) and "debits=debits+%s,reserved=reserved-%s" in query:
                query = query.replace("reserved=reserved-%s", "reserved=reserved-0*%s")
                altered.append(True)
            return super().execute(query, params, **kwargs)

    with seeded_db.connect("worker") as conn:
        conn.cursor_factory = MutantCursor
        result = payout_resolution.receive_observation(
            conn,
            command.workspace_id,
            command.operation_id,
            event(seeded_db, command),
            trust=trust(command),
            clock=lambda: NOW,
        )
    assert altered == [True] and result.status == "APPLIED"
    with pytest.raises(AssertionError, match="consumed hold must leave zero reserve"):
        assert tuple(money(seeded_db, golden)[:3]) == (98, 0, 98), (
            "consumed hold must leave zero reserve"
        )
    assert tuple(money(seeded_db, golden)[:3]) == (98, 62, 36)
    with seeded_db.connect("reader") as conn:
        report = verify_snapshot(capture_snapshot(conn, str(command.workspace_id)))
    assert report.structure_ok and not report.projection_ok


def test_bypassing_workspace_barrier_allows_frozen_write_and_literal_oracle_detects_it(
    seeded_db, golden, monkeypatch
):
    command = prepared_payout(seeded_db, golden)
    with seeded_db.connect("maintenance") as conn:
        freeze_workspace(conn, str(command.workspace_id), str(uuid4()), str(uuid4()), "mutant test")
    before = resolution_state(seeded_db)

    @contextmanager
    def no_barrier(conn, _workspace):
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            yield cur

    with monkeypatch.context() as patch:
        patch.setattr(payout_resolution, "_transaction", no_barrier)
        assert observe(seeded_db, command, event(seeded_db, command)).status == "APPLIED"
    with pytest.raises(AssertionError, match="frozen workspace must retain all financial rows"):
        assert resolution_state(seeded_db) == before, (
            "frozen workspace must retain all financial rows"
        )
    assert tuple(money(seeded_db, golden)[:3]) == (18, 0, 18)
    with seeded_db.connect("reader") as conn:
        assert conn.execute("SELECT mode FROM ledger.workspace").fetchone() == ("FROZEN",)
