"""Current run integrity and target binding on real PostgreSQL and object files."""

import json
import os
from uuid import uuid4

import pytest
from psycopg.rows import dict_row

from ledgerops import operator_evidence
from ledgerops.export import LoadedExport
from ledgerops.operator_evidence import assert_target_current, load_action_evidence
from ledgerops.reconciliation import StatementTrust, prepare_inputs, publication
from provider_sim.settlement import SettlementStore
from provider_sim.store import ProviderStore
from tests.integration.payout_helpers import NAMESPACE
from tests.integration.test_operator_actions import operator_story
from tests.integration.test_reconciliation_publication import (
    REVISION,
    accept_all,
    begin,
)
from tests.integration.test_reconciliation_publication import (
    inputs as inputs,
)
from tests.integration.test_reconciliation_publication import (
    publication_db as publication_db,
)
from tests.provider.test_process import SECRET


def complete(connect, prepared, raws, store):
    run = begin(connect, prepared, raws, store)
    accept_all(connect, prepared, store, run)
    with connect() as conn:
        result = publication.complete_run(
            conn, prepared.input_manifest["workspace_id"], store, prepared, run_id=run
        )
    assert result.published
    return run


def load(conn, prepared, run, store):
    with conn.cursor(row_factory=dict_row) as cur:
        return load_action_evidence(
            cur, prepared.input_manifest["workspace_id"], NAMESPACE, run, store
        )


def test_current_source_all_bytes_and_explicit_reader_pin_cleanup(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    run = complete(connect, prepared, raws, store)
    with database.connect("reader") as conn, conn.transaction():
        evidence = load(conn, prepared, run, store)
        assert evidence.evidence_epoch == 1
        assert (
            evidence.ledger_data_sha256
            == prepared.input_manifest["inputs"]["ledger_data"]["sha256"]
        )
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='READER'"
        ).fetchone() == (0,)
    with (
        database.connect("operator") as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        target = assert_target_current(cur, evidence, next(iter(prepared.facts.payouts)))
        assert target["projection_epoch"] == evidence.snapshot["workspace"]["projection_epoch"]


def test_same_source_new_current_run_rejects_old_run(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    old = complete(connect, prepared, raws, store)
    new = complete(connect, prepared, raws, store)
    assert old != new
    with database.connect("reader") as conn, conn.transaction():
        with pytest.raises(publication.PublicationError, match="STALE_EVIDENCE_RUN"):
            load(conn, prepared, old, store)
    with database.connect("reader") as conn, conn.transaction():
        assert load(conn, prepared, new, store).evidence_epoch == 2


def test_account_version_changed_after_capture_rejects_target(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    run = complete(connect, prepared, raws, store)
    with database.connect("reader") as conn, conn.transaction():
        evidence = load(conn, prepared, run, store)
    with database.connect("owner") as conn:
        conn.execute("UPDATE ledger.account_balance SET version=version+1")
    with (
        database.connect("operator") as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        with pytest.raises(publication.PublicationError, match="STALE_EVIDENCE_TARGET"):
            assert_target_current(cur, evidence, next(iter(prepared.facts.payouts)))


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_source_has_no_run_fallback_and_releases_pin(
    publication_db, inputs, failure
):
    database, connect = publication_db
    prepared, raws, store = inputs
    run = complete(connect, prepared, raws, store)
    digest = prepared.input_manifest["inputs"]["statement"]["sha256"]
    path = store.root / prepared.input_manifest["workspace_id"] / digest
    if failure == "missing":
        path.unlink()
    else:
        os.chmod(path, 0o600)
        path.write_bytes(b"x" * len(path.read_bytes()))
    with database.connect("reader") as conn, conn.transaction():
        with pytest.raises(publication.PublicationError, match="MISSING_OBJECT|CORRUPT_OBJECT"):
            load(conn, prepared, run, store)
        assert conn.execute(
            "SELECT count(*) FROM ledger.evidence_pin WHERE owner_type='READER'"
        ).fetchone() == (0,)


def test_wrong_namespace_never_binds_current_run(publication_db, inputs):
    database, connect = publication_db
    prepared, raws, store = inputs
    run = complete(connect, prepared, raws, store)
    with (
        database.connect("reader") as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        with pytest.raises(publication.PublicationError, match="EVIDENCE_NAMESPACE_MISMATCH"):
            load_action_evidence(cur, prepared.input_manifest["workspace_id"], uuid4(), run, store)


def test_evidence_budget_is_checked_before_reading_objects(publication_db, inputs, monkeypatch):
    database, connect = publication_db
    prepared, raws, store = inputs
    run = complete(connect, prepared, raws, store)
    monkeypatch.setattr(operator_evidence, "MAX_ACTION_EVIDENCE_BYTES", 1)

    def forbidden_read(*args):
        raise AssertionError("over-budget evidence must not be read")

    monkeypatch.setattr(store, "read", forbidden_read)
    with database.connect("reader") as conn, conn.transaction():
        with pytest.raises(publication.PublicationError, match="ACTION_EVIDENCE_CAPACITY"):
            load(conn, prepared, run, store)


def test_current_run_with_provider_inventory_before_success_cannot_authorize_posting(
    seeded_db, golden, tmp_path
):
    story = operator_story(seeded_db, golden, tmp_path)
    provider = ProviderStore(tmp_path / "older-provider.sqlite3", NAMESPACE, "test-v1", SECRET)
    statement = json.dumps(
        SettlementStore(provider).statement(
            str(story["workspace"]), story["prepared"].input_manifest["business_date"], 1
        )
    ).encode()
    raws = {**story["raws"], "statement": statement}
    bundle = LoadedExport(raws["ledger_manifest"], raws["ledger_data"])
    prepared = prepare_inputs(
        bundle,
        statement,
        trust=StatementTrust(NAMESPACE, {"test-v1": SECRET}),
        code_revision=REVISION,
        dirty=True,
        partitions=1,
    )

    def connect():
        return seeded_db.connect("reconciler")

    run = complete(connect, prepared, raws, story["store"])
    with (
        seeded_db.connect("operator") as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        evidence = load_action_evidence(cur, story["workspace"], NAMESPACE, run, story["store"])
        assert evidence.evidence_epoch == 2
        with pytest.raises(publication.PublicationError, match="EVIDENCE_PROVIDER_EFFECT_MISSING"):
            assert_target_current(cur, evidence, story["payout_id"])
