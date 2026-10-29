"""Actual PostgreSQL coverage, snapshot and bounded-disk format10 producer evidence."""

import hashlib
import json
from threading import Event
from uuid import uuid4

import pytest

from ledgerops.db.payout_resolution import receive_observation
from ledgerops.export import _SORT_KEYS, ExportLimitExceeded, capture_snapshot
from ledgerops.stream_export import export_stream
from tests.integration.concurrency_helpers import finish_call, start_call
from tests.integration.payout_helpers import admit, payout_input
from tests.integration.payout_observation_helpers import prepared_payout, trust
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.test_reconciliation_publication import REVISION

pytestmark = pytest.mark.integration


def read(path):
    raw = (path / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    assert manifest["format_version"] == 10 and manifest["row_format_version"] == 9
    assert manifest["total_bytes"] == len(raw) + sum(chunk["bytes"] for chunk in manifest["chunks"])
    rows = {table: [] for table in manifest["row_counts"] if table != "workspace"}
    for chunk in manifest["chunks"]:
        content = (path / chunk["file"]).read_bytes()
        assert len(content) == chunk["bytes"]
        assert hashlib.sha256(content).hexdigest() == chunk["sha256"]
        decoded = [json.loads(row) for row in content.splitlines()]
        assert len(decoded) == chunk["rows"]
        assert len(content) <= 1024 * 1024 or (
            len(decoded) == 1 and len(content) <= 4 * 1024 * 1024
        )
        keys = [list(row[key] for key in _SORT_KEYS[chunk["table"]]) for row in decoded]
        assert keys[0] == chunk["first_key"] and keys[-1] == chunk["last_key"]
        rows[chunk["table"]].extend(decoded)
    assert all(
        len(rows[table]) == count
        for table, count in manifest["row_counts"].items()
        if table != "workspace"
    )
    assert manifest["total_rows"] == 1 + sum(map(len, rows.values()))
    return manifest, rows


def publish(db, golden, root):
    with db.connect("reader") as conn:
        return export_stream(
            conn, golden["workspace"]["id"], root, code_revision=REVISION, dirty=True
        )


def test_full_stream_matches_legacy_rows_and_zero_table_coverage(seeded_db, golden, tmp_path):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    path = publish(seeded_db, golden, tmp_path)
    manifest, rows = read(path)
    with seeded_db.connect("reader") as conn:
        baseline = capture_snapshot(conn, golden["workspace"]["id"])
    for table, values in rows.items():
        assert values == baseline[table]
    assert manifest["source"]["workspace"] == baseline["workspace"]
    assert manifest["source"]["schema_versions"][-1] == "010_operator_actions.sql"
    assert manifest["row_counts"]["payouts"] == 0
    assert not any(chunk["table"] == "payouts" for chunk in manifest["chunks"])
    assert {path.name for path in tmp_path.iterdir()} == {manifest["export_id"]}


def test_more_than_ten_thousand_request_keys_are_complete_and_use_named_bounded_fetch(
    seeded_db, golden, tmp_path
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    with seeded_db.connect("owner") as conn:
        conn.execute(
            "INSERT INTO ledger.request_result(workspace_id,client_id,route_family,key,"
            "operation_id,payload,payload_sha256,http_status,response_body) "
            "SELECT workspace_id,client_id,route_family,'bulk-'||g::text,operation_id,"
            "payload,payload_sha256,http_status,response_body FROM ledger.request_result "
            "CROSS JOIN generate_series(1,10005) g"
        )
    named = []

    class Observed:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def cursor(self, *args, **kwargs):
            cursor = self.conn.cursor(*args, **kwargs)
            if kwargs.get("name"):
                named.append(cursor.name)

                class Cursor:
                    def __enter__(self):
                        cursor.__enter__()
                        return self

                    def __exit__(self, *args):
                        return cursor.__exit__(*args)

                    def execute(self, *args, **kwargs):
                        return cursor.execute(*args, **kwargs)

                    def fetchmany(self, size):
                        assert size == 16
                        return cursor.fetchmany(size)

                return Cursor()
            return cursor

    with seeded_db.connect("reader") as conn:
        with pytest.raises(ExportLimitExceeded):
            capture_snapshot(conn, golden["workspace"]["id"])
        path = export_stream(
            Observed(conn), golden["workspace"]["id"], tmp_path, code_revision=REVISION
        )
    manifest, rows = read(path)
    assert manifest["row_counts"]["requests"] == 10006
    assert len(rows["requests"]) == 10006 and len(named) == len(rows)
    assert len([chunk for chunk in manifest["chunks"] if chunk["table"] == "requests"]) > 1


def test_129_maximum_quarantine_payloads_stream_past_legacy_byte_cap(seeded_db, golden, tmp_path):
    command = prepared_payout(seeded_db, golden)
    second = payout_input(
        golden, key="small-second-payout", operation_id=str(uuid4()), principal_minor="1"
    )
    assert admit(seeded_db, golden, second).http_status == 202
    expected = set()
    with seeded_db.connect("worker") as conn:
        for number in range(129):
            raw = number.to_bytes(2, "big") + b"x" * 65534
            selected = command if number < 65 else second[1]
            result = receive_observation(
                conn,
                selected.workspace_id,
                selected.operation_id,
                raw,
                trust=trust(selected),
                channel="CALLBACK",
            )
            assert result.status == "QUARANTINED"
            expected.add(raw.hex())
    with seeded_db.connect("reader") as conn:
        with pytest.raises(ExportLimitExceeded):
            capture_snapshot(conn, golden["workspace"]["id"])
    manifest, rows = read(publish(seeded_db, golden, tmp_path))
    assert manifest["row_counts"]["observation_quarantines"] == 129
    assert {row["raw_payload_hex"] for row in rows["observation_quarantines"]} == expected
    assert {row["payout_id"] for row in rows["observation_quarantines"]} == {
        str(command.operation_id),
        str(second[1].operation_id),
    }
    assert manifest["total_bytes"] > 16 * 1024 * 1024


def test_repeatable_read_covers_later_chunks_while_purchase_commits(seeded_db, golden, tmp_path):
    read_balances, release = Event(), Event()

    class Observed:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def cursor(self, *args, **kwargs):
            cursor = self.conn.cursor(*args, **kwargs)
            if kwargs.get("name"):

                class Cursor:
                    target = False

                    def __enter__(self):
                        cursor.__enter__()
                        return self

                    def __exit__(self, *args):
                        return cursor.__exit__(*args)

                    def execute(self, query, *args, **kwargs):
                        self.target = "FROM ledger.account_balance " in query
                        return cursor.execute(query, *args, **kwargs)

                    def fetchmany(self, size):
                        rows = cursor.fetchmany(size)
                        if self.target and not rows:
                            read_balances.set()
                            assert release.wait(5)
                        return rows

                return Cursor()
            return cursor

    with seeded_db.connect("reader") as conn:
        worker = start_call(
            lambda: export_stream(
                Observed(conn), golden["workspace"]["id"], tmp_path, code_revision=REVISION
            )
        )
        try:
            assert read_balances.wait(5)
            assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
        finally:
            release.set()
        outcome, path = finish_call(worker)
        assert outcome == "returned"
    manifest, rows = read(path)
    assert manifest["row_counts"]["operations"] == 1
    merchant = golden["purchase_request"]["merchant_account_id"]
    assert next(row for row in rows["balances"] if row["account_id"] == merchant)["booked"] == "0"


@pytest.mark.parametrize("fault", ["write", "rows", "disk", "oversize"])
def test_failures_remove_only_owned_unpublished_stage(
    seeded_db, golden, tmp_path, monkeypatch, fault
):
    import ledgerops.stream_export as stream

    sentinel = tmp_path / "preexisting"
    sentinel.mkdir()
    (sentinel / "keep").write_bytes(b"keep")
    if fault == "write":
        original = stream._write_file
        calls = []

        def fail(path, raw):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("injected publication failure")
            return original(path, raw)

        monkeypatch.setattr(stream, "_write_file", fail)
    elif fault == "rows":
        monkeypatch.setattr(stream, "MAX_TOTAL_ROWS", 2)
    elif fault == "disk":
        monkeypatch.setattr(stream, "MAX_TOTAL_BYTES", 100)
    else:
        with seeded_db.connect("admin") as conn:
            conn.execute("ALTER TABLE ledger.operation DISABLE TRIGGER USER")
            try:
                conn.execute(
                    "UPDATE ledger.operation SET response_body="
                    "jsonb_build_object('huge',repeat('x',4194304))"
                )
            finally:
                conn.execute("ALTER TABLE ledger.operation ENABLE TRIGGER USER")
    with pytest.raises((ExportLimitExceeded, OSError)):
        publish(seeded_db, golden, tmp_path)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert (sentinel / "keep").read_bytes() == b"keep"


def test_one_large_audited_row_uses_single_bounded_oversize_chunk(seeded_db, golden, tmp_path):
    with seeded_db.connect("admin") as conn:
        conn.execute("ALTER TABLE ledger.operation DISABLE TRIGGER USER")
        try:
            conn.execute(
                "UPDATE ledger.operation SET response_body="
                "jsonb_build_object('large_serialization_fixture',repeat('x',1200000))"
            )
        finally:
            conn.execute("ALTER TABLE ledger.operation ENABLE TRIGGER USER")
    manifest, rows = read(publish(seeded_db, golden, tmp_path))
    chunk = next(chunk for chunk in manifest["chunks"] if chunk["table"] == "operations")
    assert chunk["rows"] == 1 and 1024 * 1024 < chunk["bytes"] <= 4 * 1024 * 1024
    assert len(rows["operations"][0]["response_body"]["large_serialization_fixture"]) == 1200000


def test_parent_fsync_failure_retains_complete_published_uuid_for_inspection(
    seeded_db, golden, tmp_path, monkeypatch
):
    import ledgerops.stream_export as stream

    original = stream._sync_directory

    def fail_parent(path):
        if path == tmp_path:
            raise OSError("injected parent fsync acknowledgement failure")
        return original(path)

    monkeypatch.setattr(stream, "_sync_directory", fail_parent)
    with pytest.raises(OSError) as caught:
        publish(seeded_db, golden, tmp_path)
    final = tmp_path / caught.value.export_id
    manifest, _ = read(final)
    assert final.name == manifest["export_id"]
    assert list(tmp_path.iterdir()) == [final]
