"""Kill the real exporter before publication; retain old complete exports unchanged."""

import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from ledgerops.export import ExportError, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.purchase_helpers import invoke, purchase_input

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
CHILD = r"""
import json, os, sys
from pathlib import Path
import psycopg
from ledgerops import export

original = export._write_file
def stop_after_fsync(path, data):
    original(path, data)
    if path.name == os.environ['TEST_EXPORT_STOP_MEMBER']:
        print(json.dumps({'temporary': str(path.parent), 'fsynced_member': path.name}), flush=True)
        if sys.stdin.buffer.read(1) != b'c':
            raise RuntimeError('test controller ended without publication permission')
export._write_file = stop_after_fsync
with psycopg.connect(os.environ['TEST_EXPORT_READER_DSN'], autocommit=True) as conn:
    export.export_workspace(conn, os.environ['TEST_EXPORT_WORKSPACE'],
        Path(os.environ['TEST_EXPORT_ROOT']), code_revision=os.environ['TEST_EXPORT_REVISION'])
"""


@pytest.mark.parametrize("member", ["data.json", "manifest.json"])
def test_killed_exporter_never_publishes_partial_bundle_or_changes_old_exports(
    seeded_db, golden, tmp_path, member
):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    workspace = golden["workspace"]["id"]
    revision = "82fa8ffef3ae0c2cfe5fd5883c58ab9a73cc4595"
    with seeded_db.connect("reader") as reader:
        completed = export_workspace(reader, workspace, tmp_path, code_revision=revision)
    old_bytes = {p.name: p.read_bytes() for p in completed.iterdir()}
    settings = psycopg.conninfo.make_conninfo(
        host="127.0.0.1",
        port=seeded_db.port,
        dbname=seeded_db.name,
        user="ledger_reader",
        password=seeded_db.reader_password,
        connect_timeout=2,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "TEST_EXPORT_READER_DSN": settings,
            "TEST_EXPORT_WORKSPACE": workspace,
            "TEST_EXPORT_ROOT": str(tmp_path),
            "TEST_EXPORT_REVISION": revision,
            "TEST_EXPORT_STOP_MEMBER": member,
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "exporter did not reach its fsynced-file handshake"
        line = process.stdout.readline()
        assert line, "exporter exited before the intended interruption point"
        observed = json.loads(line)
        assert observed["fsynced_member"] == member
        temporary = Path(observed["temporary"])
        assert temporary.parent == tmp_path and temporary.name.startswith(".ledgerops-export-")
        assert process.poll() is None
        process.kill()
        assert process.wait(timeout=10) < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert {p.name for p in temporary.iterdir()} == (
        {"data.json"} if member == "data.json" else {"data.json", "manifest.json"}
    )
    assert set(tmp_path.iterdir()) == {completed, temporary}
    with pytest.raises(ExportError):
        load_export(temporary)
    assert {p.name: p.read_bytes() for p in completed.iterdir()} == old_bytes
    assert verify_snapshot(load_export(completed)).healthy
