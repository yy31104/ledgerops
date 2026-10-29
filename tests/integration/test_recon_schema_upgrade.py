"""Publication schema is additive and does not invalidate retained financial exports."""

from dataclasses import replace
from importlib.resources import files
from uuid import uuid4

import pytest
from psycopg import sql

from ledgerops.db import migrations
from ledgerops.export import capture_snapshot, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.helpers import post_fixture, seed_accounts

pytestmark = pytest.mark.integration


def test_upgrade_preserves_v6_bytes_and_money_while_new_exports_declare_v8(
    postgres_cluster, golden, tmp_path, monkeypatch
):
    old = tmp_path / "old-resources"
    (old / "sql").mkdir(parents=True)
    for path in files("ledgerops.db").joinpath("sql").iterdir():
        if path.name[:3] in {"001", "002", "003", "004", "005", "006", "007"}:
            (old / "sql" / path.name).write_bytes(path.read_bytes())
    db = replace(postgres_cluster, name=f"recon_upgrade_{uuid4().hex}")
    with postgres_cluster.connect("admin") as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER ledger_migrator").format(sql.Identifier(db.name))
        )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "files", lambda package: old)
            with db.connect("admin") as conn:
                assert len(migrations.apply_migrations(conn)) == 7
        seed_accounts(db, golden)
        post_fixture(db, golden, "OPENING")
        with db.connect("reader") as conn:
            legacy = export_workspace(
                conn,
                golden["workspace"]["id"],
                tmp_path / "exports",
                code_revision="3c535a7a2fa2251aadcabe4554c67d54f49e0034",
            )
        original = {name: (legacy / name).read_bytes() for name in ("manifest.json", "data.json")}
        before = load_export(legacy)
        assert before["format_version"] == 6 and verify_snapshot(before).healthy
        with db.connect("admin") as conn:
            assert migrations.apply_migrations(conn) == [
                "008_reconciliation_publication.sql",
                "009_authentication.sql",
            ]
        with db.connect("reader") as conn:
            after = capture_snapshot(conn, golden["workspace"]["id"])
        assert after["format_version"] == 8 and verify_snapshot(after).healthy
        for key in before.keys() - {"format_version", "schema_versions", "snapshot_witness"}:
            assert before[key] == after[key], key
        assert original == {name: (legacy / name).read_bytes() for name in original}
        assert verify_snapshot(load_export(legacy)).healthy
    finally:
        with postgres_cluster.connect("admin") as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db.name)))
