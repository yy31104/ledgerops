"""Real upgrade from the historical five-migration database, preserving all sources."""

from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

import pytest
from psycopg import errors, sql

from ledgerops.db import migrations
from tests.integration.helpers import post_fixture, seed_accounts

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("legacy_reserved", [0, 10])
def test_upgrade_preserves_old_balances_or_refuses_unattributed_legacy_reserve(
    postgres_cluster, golden, tmp_path, monkeypatch, legacy_reserved
):
    old_resources = tmp_path / "old"
    sql_directory = old_resources / "sql"
    sql_directory.mkdir(parents=True)
    for path in files("ledgerops.db").joinpath("sql").iterdir():
        if path.name[:3] in {"001", "002", "003", "004", "005"}:
            (sql_directory / path.name).write_bytes(path.read_bytes())
    cluster = postgres_cluster
    db = replace(cluster, name=f"ledgerops_upgrade_{uuid4().hex}")
    with cluster.connect("admin") as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER ledger_migrator").format(sql.Identifier(db.name))
        )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "files", lambda package: old_resources)
            with db.connect("admin") as conn:
                assert len(migrations.apply_migrations(conn)) == 5
        seed_accounts(db, golden)
        post_fixture(db, golden, "OPENING")
        with db.connect("owner") as conn:
            conn.execute(
                "UPDATE ledger.account_balance SET reserved=%s WHERE account_id=%s",
                (legacy_reserved, golden["purchase_request"]["customer_account_id"]),
            )
            before = conn.execute(
                "SELECT * FROM ledger.account_balance ORDER BY account_id"
            ).fetchall()
        with db.connect("admin") as conn:
            if legacy_reserved:
                with pytest.raises(errors.CheckViolation, match="zero legacy reservations"):
                    migrations.apply_migrations(conn)
                assert conn.execute("SELECT to_regclass('ledger.payout')").fetchone() == (None,)
                assert conn.execute("SELECT count(*) FROM ledger.schema_migration").fetchone() == (
                    5,
                )
            else:
                assert migrations.apply_migrations(conn) == [
                    "006_payout_admission.sql",
                    "007_payout_resolution.sql",
                    "008_reconciliation_publication.sql",
                    "009_authentication.sql",
                ]
                assert conn.execute("SELECT count(*) FROM ledger.reservation_event").fetchone() == (
                    0,
                )
                assert migrations.apply_migrations(conn) == []
            assert (
                conn.execute("SELECT * FROM ledger.account_balance ORDER BY account_id").fetchall()
                == before
            )
    finally:
        with cluster.connect("admin") as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db.name)))
